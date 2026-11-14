#!/usr/bin/env python3
"""小数据验收：

1. 向量化引擎在批大小 1 / 3 / 全集 下的规范结果（值/NULL/错误）必须一致；
2. 独立标量解释器逐单元格结果必须与向量化引擎一致；
3. 每行每表达式节点的求值集合（trace）跨批大小、跨实现一致；
4. 现成数据上的关键语义点：未选择的坏文本、NULL 除数、实际除零、
   最大整数加一、未知逻辑值；
5. 合成小数据覆盖 INT64_MIN/-1、向零截断、to_int 词法边界、三值逻辑；
6. 预检必须拒绝结构/类型错误，而不是退化为逐行 NULL。

用法: python3 acceptance.py [--fixtures fixtures] [--out out]
退出码非 0 表示存在失败项。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from etl.compiler import compile_all
from etl.engine import Engine, Trace
from etl.loader import load_columns, load_expressions, load_schema
from etl.model import INT64_MAX, INT64_MIN, CompileError
from etl.scalar import run_scalar

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))


# ---------------------------------------------------------------- 工具

def _tag(v):
    if isinstance(v, bool):
        return ("bool", v)
    if isinstance(v, int):
        return ("int", v)
    return ("text", v)


def canonical(outputs: dict[str, list[dict]]) -> dict[str, list]:
    """逐单元格规范形式：(带类型的值, is_null, (错误码, 节点路径)|None)。"""
    canon = {}
    for name, rows in outputs.items():
        canon[name] = [
            (
                None if rec["error"] or rec["is_null"] else _tag(rec["value"]),
                rec["is_null"],
                None if rec["error"] is None
                else (rec["error"]["code"], rec["error"]["node_path"]),
            )
            for rec in rows
        ]
    return canon


def diff_cells(a, b, exprs) -> str:
    msgs = []
    for name in exprs:
        for i, (ca, cb) in enumerate(zip(a[name], b[name])):
            if ca != cb:
                msgs.append(f"{name} 行{i}: {ca} != {cb}")
                if len(msgs) >= 5:
                    return "; ".join(msgs) + "; ..."
    return "; ".join(msgs)


def run_vectorized(plans, columns, schema, n, batch_size):
    trace = Trace()
    engine = Engine(plans, columns, schema, n, batch_size, trace)
    outputs = engine.run()
    return canonical(outputs), {name: trace.eval_set(name) for name in plans}


def expect_cell(canon, expr, row, value=None, is_null=False, error=None):
    """value 用 _tag 形式；error 为 (code, path)。"""
    got = canon[expr][row]
    want = (value, is_null, error)
    return got == want, f"期望 {want}, 实际 {got}"


# ---------------------------------------------------------------- 1. 现成数据

def fixture_checks(fx: Path) -> None:
    schema = load_schema(fx / "schema.json")
    columns, n = load_columns(fx / "columns.json", schema)
    exprs = load_expressions(fx / "expressions.json")
    plans = compile_all(exprs, schema)
    names = list(plans)

    canon_by_bs, sets_by_bs = {}, {}
    for bs in (1, 3, n):
        canon_by_bs[bs], sets_by_bs[bs] = run_vectorized(
            plans, columns, schema, n, bs)

    s_trace = Trace()
    s_outputs, _ = run_scalar(plans, columns, n, s_trace)
    canon_scalar = canonical(s_outputs)
    sets_scalar = {name: s_trace.eval_set(name) for name in plans}

    # 跨批大小一致
    check("fixtures: 批大小1 vs 3 规范结果一致",
          canon_by_bs[1] == canon_by_bs[3],
          diff_cells(canon_by_bs[1], canon_by_bs[3], names))
    check("fixtures: 批大小3 vs 全集 规范结果一致",
          canon_by_bs[3] == canon_by_bs[n],
          diff_cells(canon_by_bs[3], canon_by_bs[n], names))
    check("fixtures: 批大小1/3/全集 节点求值集合一致",
          sets_by_bs[1] == sets_by_bs[3] == sets_by_bs[n],
          "")
    # 向量化 vs 标量解释器
    check("fixtures: 向量化引擎 vs 标量解释器 逐单元格一致",
          canon_by_bs[3] == canon_scalar,
          diff_cells(canon_by_bs[3], canon_scalar, names))
    check("fixtures: 向量化引擎 vs 标量解释器 节点求值集合一致",
          sets_by_bs[3] == sets_scalar,
          "")

    c = canon_by_bs[3]
    es = sets_by_bs[3]

    # ---- 未选择的坏文本：IF 另一分支 / 短路 / COALESCE 已命中均不触发 to_int
    ok, d = expect_cell(c, "branch_cast", 1, ("int", 7))
    check("未选择的坏文本: branch_cast r1 走 else 得 7，不报 to_int(bad)", ok, d)
    check("未选择的坏文本: branch_cast 的 to_int($/1) 只求值 {r0,r3,r5}",
          es["branch_cast"] == {("$", i) for i in range(8)} |
                               {("$/1", i) for i in (0, 3, 5)},
          f"实际 {sorted(es['branch_cast'])}")
    ok, d = expect_cell(c, "guarded_and", 7, ("bool", False))
    check("未选择的坏文本: guarded_and r7 左 FALSE 短路，不求 to_int(03)", ok, d)
    check("未选择的坏文本: guarded_and 右侧($/1,$/1/0)只求值 {r1,r4,r5}",
          es["guarded_and"] == {("$", i) for i in range(8)} |
                               {("$/0", i) for i in range(8)} |
                               {("$/1", i) for i in (1, 4, 5)} |
                               {("$/1/0", i) for i in (1, 4, 5)},
          f"实际 {sorted(es['guarded_and'])}")
    ok, d = expect_cell(c, "fallback", 5, ("int", 8))
    check("未选择的坏文本: fallback r5 首项非 NULL，不求 to_int(bad)", ok, d)
    check("未选择的坏文本: fallback 的 to_int($/1) 只求值 {r2}",
          es["fallback"] == {("$", i) for i in range(8)} | {("$/1", 2)},
          f"实际 {sorted(es['fallback'])}")

    # ---- NULL 除数：结果是 NULL，不是错误
    ok, d = expect_cell(c, "unguarded_div", 5, is_null=True)
    check("NULL 除数: unguarded_div r5 (b=NULL) 结果为 NULL 而非错误", ok, d)
    ok, d = expect_cell(c, "guarded", 5, is_null=True)
    check("NULL 除数: guarded r5 (eq=NULL→else, b=NULL) 结果为 NULL", ok, d)

    # ---- 实际除零：只在被选中分支报错，错误带 row 与 AST 路径
    ok, d = expect_cell(c, "unguarded_div", 1, error=("div_zero", "$"))
    check("实际除零: unguarded_div r1 报 div_zero@$", ok, d)
    ok, d = expect_cell(c, "unguarded_div", 4, error=("div_zero", "$"))
    check("实际除零: unguarded_div r4 报 div_zero@$", ok, d)
    ok, d = expect_cell(c, "guarded", 1, ("int", 0))
    check("实际除零: guarded r1 被 IF 保护得 0，div 未求值该行", ok, d)
    ok, d = expect_cell(c, "guarded", 4, ("int", 0))
    check("实际除零: guarded r4 被 IF 保护得 0", ok, d)
    check("实际除零: guarded 的 div($/2) 求值集合排除 {r1,r4}",
          es["guarded"] == {("$", i) for i in range(8)} |
                           {("$/0", i) for i in range(8)} |
                           {("$/2", i) for i in (0, 2, 3, 5, 6, 7)},
          f"实际 {sorted(es['guarded'])}")

    # ---- 最大整数加一：只影响该表达式该行，不污染同 row 的其他表达式
    ok, d = expect_cell(c, "overflow", 3, error=("overflow", "$"))
    check("最大整数加一: overflow r3 报 overflow@$", ok, d)
    ok, d = expect_cell(c, "guarded", 3, ("int", INT64_MAX))
    check("最大整数加一: 同行的 guarded r3 不受影响 (INT64_MAX/1)", ok, d)
    ok, d = expect_cell(c, "fallback", 3, ("int", INT64_MAX))
    check("最大整数加一: 同行的 fallback r3 不受影响", ok, d)

    # ---- 未知逻辑值：UNKNOWN 仍需右侧；IF 的 NULL 条件选 else
    ok, d = expect_cell(c, "guarded_and", 5, error=("bad_to_int", "$/1/0"))
    check("未知逻辑值: guarded_and r5 左 UNKNOWN 求右侧，to_int(bad) 报错", ok, d)
    ok, d = expect_cell(c, "guarded_or", 5, error=("bad_to_int", "$/1/0"))
    check("未知逻辑值: guarded_or r5 左 UNKNOWN 求右侧，to_int(bad) 报错", ok, d)
    ok, d = expect_cell(c, "guarded_or", 7, error=("bad_to_int", "$/1/0"))
    check("未知逻辑值: guarded_or r7 左 FALSE 求右侧，to_int(03) 报词法错", ok, d)
    ok, d = expect_cell(c, "branch_cast", 2, ("int", 7))
    check("未知逻辑值: branch_cast r2 条件 NULL 选 else 得 7", ok, d)
    ok, d = expect_cell(c, "branch_cast", 6, ("int", 7))
    check("未知逻辑值: branch_cast r6 条件 NULL 选 else 得 7", ok, d)

    # ---- 错误独立性：bad_to_int 只落在对应表达式对应行
    ok, d = expect_cell(c, "branch_cast", 5, error=("bad_to_int", "$/1"))
    check("错误独立: branch_cast r5 报 bad_to_int@$/1", ok, d)
    ok, d = expect_cell(c, "fallback", 5, ("int", 8))
    check("错误独立: 同行的 fallback r5 不受 branch_cast 错误影响", ok, d)
    ok, d = expect_cell(c, "guarded", 2, is_null=True)
    check("NULL 传播: guarded r2 (a=NULL) else 分支 div 得 NULL", ok, d)


# ---------------------------------------------------------------- 2. 合成小数据

def synthetic_checks() -> None:
    schema = {
        "rid": ("text", False),
        "x": ("int64", True),
        "y": ("int64", True),
        "t": ("text", True),
        "flag": ("bool", True),
    }
    columns = {
        "rid": [f"s{i}" for i in range(8)],
        "x": [INT64_MIN, -7, 7, -7, INT64_MAX, None, 5, INT64_MIN],
        "y": [-1, 2, -2, -2, -1, 2, None, 1],
        "t": ["-0", "+5", " 5", str(INT64_MAX), str(INT64_MAX) + "0",
              str(INT64_MIN), "5 ", "0"],
        "flag": [True, None, False, True, False, None, True, False],
    }
    exprs = [
        {"name": "min_div", "expr": {"div": [{"col": "x"}, {"col": "y"}]}},
        {"name": "casts", "expr": {"to_int": {"col": "t"}}},
        {"name": "isnull_x", "expr": {"is_null": {"col": "x"}}},
        {"name": "not_flag", "expr": {"not": {"col": "flag"}}},
        {"name": "txt_eq", "expr": {"eq": [{"col": "t"}, {"literal": "0"}]}},
        {"name": "txt_gt", "expr": {"gt": [{"col": "t"}, {"literal": "0"}]}},
        {"name": "add_edge", "expr": {"add": [{"col": "x"}, {"literal": -1}]}},
        {"name": "coalesce_err", "expr": {"coalesce": [
            {"div": [{"col": "x"}, {"col": "y"}]}, {"literal": 0}]}},
        {"name": "and3", "expr": {"and": [{"col": "flag"}, {"literal": True}]}},
        {"name": "or3", "expr": {"or": [{"col": "flag"}, {"literal": False}]}},
        {"name": "guard_txt", "expr": {"if": [
            {"col": "flag"}, {"to_int": {"col": "t"}}, {"literal": -1}]}},
        {"name": "coalesce_null_lit", "expr": {"coalesce": [
            {"literal": None}, {"col": "x"}, {"literal": 1}]}},
        {"name": "if_null_cond", "expr": {"if": [
            {"literal": None}, {"literal": 1}, {"literal": 2}]}},
    ]
    plans = compile_all(exprs, schema)
    n = 8
    canon_by_bs, sets_by_bs = {}, {}
    for bs in (1, 3, n):
        canon_by_bs[bs], sets_by_bs[bs] = run_vectorized(
            plans, columns, schema, n, bs)
    s_trace = Trace()
    s_outputs, _ = run_scalar(plans, columns, n, s_trace)
    canon_scalar = canonical(s_outputs)
    sets_scalar = {name: s_trace.eval_set(name) for name in plans}
    names = list(plans)

    check("synthetic: 批大小1/3/全集 规范结果一致",
          canon_by_bs[1] == canon_by_bs[3] == canon_by_bs[n],
          diff_cells(canon_by_bs[1], canon_by_bs[3], names) or
          diff_cells(canon_by_bs[3], canon_by_bs[n], names))
    check("synthetic: 向量化引擎 vs 标量解释器 逐单元格一致",
          canon_by_bs[3] == canon_scalar,
          diff_cells(canon_by_bs[3], canon_scalar, names))
    check("synthetic: 节点求值集合跨批大小、跨实现一致",
          sets_by_bs[1] == sets_by_bs[3] == sets_by_bs[n] == sets_scalar,
          "")

    c = canon_by_bs[3]

    def cell(name, expr, row, value=None, **kw):
        ok, d = expect_cell(c, expr, row, value, **kw)
        check(name, ok, d)

    # INT64_MIN / -1 与向零截断
    cell("synthetic: INT64_MIN / -1 报 overflow", "min_div", 0,
         error=("overflow", "$"))
    cell("synthetic: -7/2 向零截断为 -3", "min_div", 1, ("int", -3))
    cell("synthetic: 7/-2 向零截断为 -3", "min_div", 2, ("int", -3))
    cell("synthetic: -7/-2 向零截断为 3", "min_div", 3, ("int", 3))
    cell("synthetic: INT64_MAX / -1 = -INT64_MAX", "min_div", 4,
         ("int", -INT64_MAX))
    cell("synthetic: NULL 被除数得 NULL", "min_div", 5, is_null=True)
    cell("synthetic: NULL 除数得 NULL", "min_div", 6, is_null=True)
    cell("synthetic: INT64_MIN / 1 = INT64_MIN", "min_div", 7, ("int", INT64_MIN))

    # to_int 词法与范围边界
    cell("synthetic: to_int('-0') = 0", "casts", 0, ("int", 0))
    cell("synthetic: to_int('+5') 词法拒绝", "casts", 1, error=("bad_to_int", "$"))
    cell("synthetic: to_int(' 5') 词法拒绝", "casts", 2, error=("bad_to_int", "$"))
    cell("synthetic: to_int(INT64_MAX) 合法", "casts", 3, ("int", INT64_MAX))
    cell("synthetic: to_int 超上限拒绝", "casts", 4, error=("bad_to_int", "$"))
    cell("synthetic: to_int(INT64_MIN) 合法", "casts", 5, ("int", INT64_MIN))
    cell("synthetic: to_int('5 ') 词法拒绝", "casts", 6, error=("bad_to_int", "$"))
    cell("synthetic: to_int('0') = 0", "casts", 7, ("int", 0))

    # is_null / not
    check("synthetic: is_null(x) 全列正确",
          [r[0] for r in c["isnull_x"]] ==
          [("bool", v) for v in (False, False, False, False, False, True, False, False)],
          str(c["isnull_x"]))
    check("synthetic: not(flag) 三值正确",
          [(r[0], r[1]) for r in c["not_flag"]] ==
          [(("bool", False), False), (None, True), (("bool", True), False),
           (("bool", False), False), (("bool", True), False), (None, True),
           (("bool", False), False), (("bool", True), False)],
          str(c["not_flag"]))

    # 文本比较（UTF-8 字节序）
    check("synthetic: txt_eq 全列正确",
          [r[0] for r in c["txt_eq"]] ==
          [("bool", v) for v in (False, False, False, False, False, False, False, True)],
          str(c["txt_eq"]))
    check("synthetic: txt_gt 按字节序",
          [r[0] for r in c["txt_gt"]] ==
          [("bool", v) for v in (False, False, False, True, True, False, True, False)],
          str(c["txt_gt"]))

    # 溢出边界
    cell("synthetic: INT64_MIN + (-1) 报 overflow", "add_edge", 0,
         error=("overflow", "$"))
    cell("synthetic: INT64_MAX + (-1) 合法", "add_edge", 4, ("int", INT64_MAX - 1))
    cell("synthetic: add NULL 传播", "add_edge", 5, is_null=True)

    # COALESCE：已发生错误不被默认值吞掉
    cell("synthetic: coalesce 不吞 overflow 错误", "coalesce_err", 0,
         error=("overflow", "$/0"))
    cell("synthetic: coalesce 首项 NULL 取默认值", "coalesce_err", 5, ("int", 0))
    cell("synthetic: coalesce 首项非 NULL 得首项", "coalesce_err", 1, ("int", -3))

    # 三值逻辑真值表抽样
    check("synthetic: AND 三值 (T/UNK/F 与 TRUE)",
          [r for r in c["and3"]] ==
          [(("bool", True), False, None), (None, True, None),
           (("bool", False), False, None), (("bool", True), False, None),
           (("bool", False), False, None), (None, True, None),
           (("bool", True), False, None), (("bool", False), False, None)],
          str(c["and3"]))
    check("synthetic: OR 三值 (T/UNK/F 或 FALSE)",
          [r for r in c["or3"]] ==
          [(("bool", True), False, None), (None, True, None),
           (("bool", False), False, None), (("bool", True), False, None),
           (("bool", False), False, None), (None, True, None),
           (("bool", True), False, None), (("bool", False), False, None)],
          str(c["or3"]))

    # IF 保护坏文本 + NULL 条件选 else
    cell("synthetic: guard_txt s0 then 分支 to_int('-0')=0", "guard_txt", 0,
         ("int", 0))
    cell("synthetic: guard_txt s1 条件 NULL 选 else", "guard_txt", 1, ("int", -1))
    cell("synthetic: guard_txt s6 then 分支 to_int('5 ') 报错", "guard_txt", 6,
         error=("bad_to_int", "$/1"))
    check("synthetic: guard_txt 的 to_int 只求值 flag=TRUE 的 {s0,s3,s6}",
          sets_by_bs[3]["guard_txt"] ==
          {("$", i) for i in range(8)} | {("$/1", i) for i in (0, 3, 6)},
          f"实际 {sorted(sets_by_bs[3]['guard_txt'])}")

    # NULL 字面量由上下文定型
    cell("synthetic: coalesce(NULL, x, 1) s5 落到默认值", "coalesce_null_lit", 5,
         ("int", 1))
    cell("synthetic: coalesce(NULL, x, 1) s0 取 x", "coalesce_null_lit", 0,
         ("int", INT64_MIN))
    cell("synthetic: if(NULL,1,2) 全行为 2", "if_null_cond", 3, ("int", 2))


# ---------------------------------------------------------------- 3. 预检拒绝

def precheck_checks(fx: Path) -> None:
    schema = load_schema(fx / "schema.json")
    bad_exprs = [
        ("bool 参与 add", {"add": [{"col": "choose"}, {"literal": 1}]}),
        ("gt 不支持 bool", {"gt": [{"literal": True}, {"literal": False}]}),
        ("if 双 NULL 分支无法定型", {"if": [
            {"col": "choose"}, {"literal": None}, {"literal": None}]}),
        ("eq(NULL,NULL) 无法定型", {"eq": [{"literal": None}, {"literal": None}]}),
        ("div 元数错误", {"div": [{"col": "a"}]}),
        ("未知 operator", {"sqrt": [{"col": "a"}]}),
        ("int 字面量超 int64", {"add": [{"col": "a"}, {"literal": 2**63}]}),
        ("未知列", {"add": [{"col": "nope"}, {"literal": 1}]}),
        ("to_int 作用于 int64", {"to_int": {"col": "a"}}),
        ("is_null(NULL) 无法定型", {"is_null": {"literal": None}}),
        ("float 字面量不支持", {"add": [{"col": "a"}, {"literal": 1.5}]}),
        ("节点含两个 operator", {"add": [{"col": "a"}, {"literal": 1}],
                              "div": [{"col": "a"}, {"literal": 1}]}),
        ("eq 两端类型不一致", {"eq": [{"col": "a"}, {"col": "text"}]}),
        ("and 作用于 int64", {"and": [{"col": "a"}, {"literal": 1}]}),
    ]
    for label, expr in bad_exprs:
        try:
            compile_all([{"name": "bad", "expr": expr}], schema)
        except CompileError:
            check(f"预检拒绝: {label}", True)
        else:
            check(f"预检拒绝: {label}", False, "未被拒绝")

    good_exprs = [
        ("NULL 字面量按上下文定型", {"coalesce": [
            {"literal": None}, {"col": "a"}, {"literal": 1}]}),
        ("if 的 NULL 条件合法", {"if": [
            {"literal": None}, {"col": "a"}, {"literal": 0}]}),
        ("is_null(col) 合法", {"is_null": {"col": "text"}}),
    ]
    for label, expr in good_exprs:
        try:
            compile_all([{"name": "ok", "expr": expr}], schema)
        except CompileError as e:
            check(f"预检通过: {label}", False, f"被误拒: {e}")
        else:
            check(f"预检通过: {label}", True)


# ---------------------------------------------------------------- 入口

def main() -> int:
    ap = argparse.ArgumentParser(description="小数据验收")
    ap.add_argument("--fixtures", default="fixtures")
    ap.add_argument("--out", default="out")
    args = ap.parse_args()

    fx = Path(args.fixtures)
    fixture_checks(fx)
    synthetic_checks()
    precheck_checks(fx)

    passed = sum(1 for _, ok, _ in CHECKS if ok)
    failed = len(CHECKS) - passed
    report = {
        "summary": {"total": len(CHECKS), "passed": passed, "failed": failed},
        "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in CHECKS],
    }
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "acceptance.json").open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")

    for name, ok, detail in CHECKS:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if not ok and detail:
            line += f" -- {detail}"
        print(line)
    print(f"\n{passed}/{len(CHECKS)} 通过, {failed} 失败 -> {out_dir}/acceptance.json")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
