"""向量化执行引擎：selection vector 驱动，逐节点在当前有效行上运算。

关键不变量：
- 每个运算节点只在其输入 selection（原始行号集合）上计算，绝不先对整列
  求值再用 np.where 掩盖异常；IF/COALESCE/AND/OR 会缩小分支的有效行集合。
- 错误不是 NULL：出错行立即从后续 selection 中剔除，错误码 + AST 路径
  记录在 BatchCtx，不会被后续分支或默认值吞掉，也不污染其他行/表达式。
- 所有运算节点的进入事件由唯一执行入口 Engine._eval 记录（行号 + 计数），
  用于证明危险分支没有被提前计算。
"""
from __future__ import annotations

import numpy as np

from .compiler import Plan
from .model import (
    ERR_BAD_TO_INT, ERR_DIV_ZERO, ERR_OVERFLOW, INT64_MAX, INT64_MIN,
    OP_NODES, TO_INT_RE, TYPE_BOOL, TYPE_INT, Node, Vector,
)


class Trace:
    """全部 batch 共享的求值事件日志：由执行入口产生。"""

    def __init__(self) -> None:
        # (表达式名, 节点路径, batch 序号, 该次进入的原始行号元组)
        self.events: list[tuple[str, str, int, tuple[int, ...]]] = []

    def record(self, expr: str, path: str, batch_id: int, rows: np.ndarray) -> None:
        self.events.append((expr, path, batch_id, tuple(int(r) for r in rows)))

    def eval_set(self, expr: str) -> set[tuple[str, int]]:
        """(节点路径, 原始行号) 集合：与批大小无关，用于跨实现核对。"""
        out: set[tuple[str, int]] = set()
        for e, path, _b, rows in self.events:
            if e == expr:
                out.update((path, r) for r in rows)
        return out

    def to_dict(self, expr_names: list[str]) -> dict:
        per_expr: dict[str, dict] = {name: {} for name in expr_names}
        for expr, path, batch_id, rows in self.events:
            nodes = per_expr[expr]
            entry = nodes.setdefault(path, {"entries": [], "total_count": 0,
                                            "rows": set()})
            entry["entries"].append({"batch": batch_id, "rows": list(rows),
                                     "count": len(rows)})
            entry["total_count"] += len(rows)
            entry["rows"].update(rows)
        for nodes in per_expr.values():
            for entry in nodes.values():
                entry["rows"] = sorted(entry["rows"])
        return per_expr


class BatchCtx:
    """单个表达式在一个 batch 上的执行上下文。"""

    def __init__(self, n: int, batch_id: int, offset: int,
                 columns: dict[str, Vector], trace: Trace, expr_name: str):
        self.n = n
        self.batch_id = batch_id
        self.offset = offset            # batch 首行的原始行号
        self.columns = columns
        self.trace = trace
        self.expr_name = expr_name
        self.error_mask = np.zeros(n, dtype=np.bool_)
        self.errors: dict[int, tuple[str, str]] = {}   # batch 内行号 -> (code, path)

    def record_error(self, positions, code: str, path: str) -> None:
        for p in positions:
            p = int(p)
            if not self.error_mask[p]:      # 首个错误生效
                self.error_mask[p] = True
                self.errors[p] = (code, path)

    def ok(self, sel: np.ndarray) -> np.ndarray:
        """从 selection 中剔除已出错行。"""
        if sel.size == 0:
            return sel
        return sel[~self.error_mask[sel]]


class Engine:
    def __init__(self, plans: dict[str, Plan], columns: dict[str, list],
                 schema: dict[str, tuple[str, bool]], n_rows: int,
                 batch_size: int, trace: Trace | None = None):
        if batch_size < 1:
            raise ValueError("batch_size 必须 >= 1")
        self.plans = plans
        self.columns = columns
        self.schema = schema
        self.n_rows = n_rows
        self.batch_size = batch_size
        self.trace = trace if trace is not None else Trace()

    # ------------------------------------------------------------ 执行入口

    def _eval(self, node: Node, sel: np.ndarray, ctx: BatchCtx) -> Vector:
        """唯一执行入口：所有运算节点经此进入并记录 trace（原始行号）。"""
        if node.op == "col":
            return ctx.columns[node.col_name]     # col 读取不是运算
        if sel.size == 0:
            return Vector.empty(node.type, ctx.n)
        if node.op == "literal":
            v = Vector.empty(node.type, ctx.n)
            if node.lit_kind == "null":
                v.null[sel] = True
            else:
                v.values[sel] = node.literal
            return v
        assert node.op in OP_NODES
        ctx.trace.record(ctx.expr_name, node.path, ctx.batch_id, sel + ctx.offset)
        return _HANDLERS[node.op](self, node, sel, ctx)

    # ------------------------------------------------------------ 批处理

    def _column_vector(self, name: str, lo: int, hi: int) -> Vector:
        base, _nullable = self.schema[name]
        data = self.columns[name][lo:hi]
        v = Vector.empty(base, hi - lo)
        if base == TYPE_INT:
            v.values[:] = np.array(
                [0 if x is None else x for x in data], dtype=np.int64)
        elif base == TYPE_BOOL:
            v.values[:] = np.array(
                [False if x is None else x for x in data], dtype=np.bool_)
        else:
            for i, x in enumerate(data):
                if x is not None:
                    v.values[i] = x
        v.null[:] = np.array([x is None for x in data], dtype=np.bool_)
        return v

    def run(self) -> dict[str, list[dict]]:
        """返回 {表达式名: [每行输出记录]}，记录含 value / is_null / error。"""
        outputs: dict[str, list] = {name: [None] * self.n_rows for name in self.plans}
        for batch_id, lo in enumerate(range(0, self.n_rows, self.batch_size)):
            hi = min(lo + self.batch_size, self.n_rows)
            n = hi - lo
            cols = {name: self._column_vector(name, lo, hi) for name in self.columns}
            for name, plan in self.plans.items():
                ctx = BatchCtx(n, batch_id, lo, cols, self.trace, name)
                root = self._eval(plan.root, np.arange(n), ctx)
                self._assemble(ctx, root, outputs[name])
        return outputs

    @staticmethod
    def _assemble(ctx: BatchCtx, root: Vector, out_rows: list) -> None:
        for i in range(ctx.n):
            g = ctx.offset + i
            if ctx.error_mask[i]:
                code, path = ctx.errors[i]
                out_rows[g] = {"value": None, "is_null": False,
                               "error": {"code": code, "node_path": path}}
            elif root.null[i]:
                out_rows[g] = {"value": None, "is_null": True, "error": None}
            else:
                val = root.values[i]
                if isinstance(val, np.generic):
                    val = val.item()
                out_rows[g] = {"value": val, "is_null": False, "error": None}


# ---------------------------------------------------------------- 节点算子
# 每个算子只处理进入 selection 的行；返回稠密 Vector（仅有效行有定义）。

def _binary_inputs(engine: Engine, node: Node, sel: np.ndarray, ctx: BatchCtx):
    """其余二元节点左至右求值：左侧出错的行不再进入右侧。"""
    left = engine._eval(node.children[0], sel, ctx)
    sel_r = ctx.ok(sel)
    right = engine._eval(node.children[1], sel_r, ctx)
    return left, right, ctx.ok(sel_r)


def _h_add(engine, node, sel, ctx) -> Vector:
    left, right, ok = _binary_inputs(engine, node, sel, ctx)
    out = Vector.empty(TYPE_INT, ctx.n)
    if ok.size:
        out.null[ok] = left.null[ok] | right.null[ok]
        nn = ok[~out.null[ok]]
        if nn.size:
            a = left.values[nn]
            b = right.values[nn]
            res = a + b                      # int64 回绕
            ovf = ((a > 0) & (b > 0) & (res < 0)) | \
                  ((a < 0) & (b < 0) & (res >= 0))
            if ovf.any():
                ctx.record_error(nn[ovf], ERR_OVERFLOW, node.path)
                good = nn[~ovf]
                out.values[good] = res[~ovf]
            else:
                out.values[nn] = res
    return out


def _h_div(engine, node, sel, ctx) -> Vector:
    left, right, ok = _binary_inputs(engine, node, sel, ctx)
    out = Vector.empty(TYPE_INT, ctx.n)
    if ok.size:
        out.null[ok] = left.null[ok] | right.null[ok]
        nn = ok[~out.null[ok]]
        if nn.size:
            a = left.values[nn]
            b = right.values[nn]
            zero = b == 0
            ovf = (a == INT64_MIN) & (b == -1)   # 唯一可能溢出的除法
            ctx.record_error(nn[zero], ERR_DIV_ZERO, node.path)
            ctx.record_error(nn[ovf & ~zero], ERR_OVERFLOW, node.path)
            good = nn[~(zero | ovf)]
            if good.size:
                av = left.values[good]
                bv = right.values[good]
                q = av // bv                   # NumPy 为向下取整
                r = av % bv
                # 修正为向零截断：余数非零且异号时 +1
                q += (r != 0) & ((av < 0) != (bv < 0))
                out.values[good] = q
    return out


def _h_to_int(engine, node, sel, ctx) -> Vector:
    src = engine._eval(node.children[0], sel, ctx)
    ok = ctx.ok(sel)
    out = Vector.empty(TYPE_INT, ctx.n)
    if ok.size:
        out.null[ok] = src.null[ok]
        nn = ok[~src.null[ok]]
        # 文本词法解析无合适 NumPy 内核：仅对选中行做局部元素转换。
        for p in nn:
            s = src.values[p]
            if TO_INT_RE.fullmatch(s) is None:
                ctx.record_error([p], ERR_BAD_TO_INT, node.path)
                continue
            val = int(s)
            if val < INT64_MIN or val > INT64_MAX:
                ctx.record_error([p], ERR_BAD_TO_INT, node.path)
                continue
            out.values[p] = val
    return out


def _compare(engine, node, sel, ctx, np_op) -> Vector:
    left, right, ok = _binary_inputs(engine, node, sel, ctx)
    out = Vector.empty(TYPE_BOOL, ctx.n)
    if ok.size:
        out.null[ok] = left.null[ok] | right.null[ok]
        nn = ok[~out.null[ok]]
        if nn.size:
            # 文本按 UTF-8 字节序比较；Python str 序与 UTF-8 字节序一致。
            res = np_op(left.values[nn], right.values[nn])
            out.values[nn] = np.asarray(res, dtype=np.bool_)
    return out


def _h_eq(engine, node, sel, ctx) -> Vector:
    return _compare(engine, node, sel, ctx, np.equal)


def _h_gt(engine, node, sel, ctx) -> Vector:
    return _compare(engine, node, sel, ctx, np.greater)


def _h_is_null(engine, node, sel, ctx) -> Vector:
    src = engine._eval(node.children[0], sel, ctx)
    ok = ctx.ok(sel)
    out = Vector.empty(TYPE_BOOL, ctx.n)
    if ok.size:
        out.values[ok] = src.null[ok]
    return out


def _h_not(engine, node, sel, ctx) -> Vector:
    src = engine._eval(node.children[0], sel, ctx)
    ok = ctx.ok(sel)
    out = Vector.empty(TYPE_BOOL, ctx.n)
    if ok.size:
        out.null[ok] = src.null[ok]
        nn = ok[~src.null[ok]]
        if nn.size:
            out.values[nn] = ~src.values[nn]
    return out


def _h_if(engine, node, sel, ctx) -> Vector:
    cond = engine._eval(node.children[0], sel, ctx)
    ok = ctx.ok(sel)
    out = Vector.empty(node.type, ctx.n)
    if ok.size:
        cv = cond.values[ok]
        cn = cond.null[ok]
        then_sel = ok[cv & ~cn]        # TRUE 选 then
        else_sel = ok[~cv | cn]        # FALSE 或 NULL 选 else
        then_v = engine._eval(node.children[1], then_sel, ctx)
        else_v = engine._eval(node.children[2], else_sel, ctx)
        t_ok = ctx.ok(then_sel)
        e_ok = ctx.ok(else_sel)
        if t_ok.size:
            out.values[t_ok] = then_v.values[t_ok]
            out.null[t_ok] = then_v.null[t_ok]
        if e_ok.size:
            out.values[e_ok] = else_v.values[e_ok]
            out.null[e_ok] = else_v.null[e_ok]
    return out


def _h_coalesce(engine, node, sel, ctx) -> Vector:
    out = Vector.empty(node.type, ctx.n)
    remaining = sel
    for child in node.children:
        remaining = ctx.ok(remaining)      # 已出错行：错误保留，不再求后续项
        if remaining.size == 0:
            return out
        v = engine._eval(child, remaining, ctx)
        remaining = ctx.ok(remaining)
        if remaining.size == 0:
            return out
        got = remaining[~v.null[remaining]]   # 取到非 NULL：不再求后续项
        out.values[got] = v.values[got]
        remaining = remaining[v.null[remaining]]
    out.null[remaining] = True             # 全部为 NULL 的行
    return out


def _h_and(engine, node, sel, ctx) -> Vector:
    left = engine._eval(node.children[0], sel, ctx)
    ok = ctx.ok(sel)
    out = Vector.empty(TYPE_BOOL, ctx.n)
    if ok.size == 0:
        return out
    lv = left.values[ok]
    ln = left.null[ok]
    out.values[ok[~lv & ~ln]] = False      # 左 FALSE：短路，不求右侧
    need = ok[lv | ln]                     # 左 TRUE 或 UNKNOWN 仍需右侧
    if need.size == 0:
        return out
    right = engine._eval(node.children[1], need, ctx)
    ok2 = ctx.ok(need)
    if ok2.size == 0:
        return out
    rv = right.values[ok2]
    rn = right.null[ok2]
    l_true = left.values[ok2] & ~left.null[ok2]
    l_null = left.null[ok2]
    res_val = np.zeros(ok2.size, dtype=np.bool_)
    res_null = np.zeros(ok2.size, dtype=np.bool_)
    res_val[l_true] = rv[l_true]           # TRUE AND x = x
    res_null[l_true] = rn[l_true]
    # UNKNOWN AND FALSE = FALSE；UNKNOWN AND (TRUE|UNKNOWN) = UNKNOWN
    res_val[l_null] = False
    res_null[l_null] = rn[l_null] | rv[l_null]
    out.values[ok2] = res_val
    out.null[ok2] = res_null
    return out


def _h_or(engine, node, sel, ctx) -> Vector:
    left = engine._eval(node.children[0], sel, ctx)
    ok = ctx.ok(sel)
    out = Vector.empty(TYPE_BOOL, ctx.n)
    if ok.size == 0:
        return out
    lv = left.values[ok]
    ln = left.null[ok]
    out.values[ok[lv & ~ln]] = True        # 左 TRUE：短路，不求右侧
    need = ok[~lv | ln]                    # 左 FALSE 或 UNKNOWN 仍需右侧
    if need.size == 0:
        return out
    right = engine._eval(node.children[1], need, ctx)
    ok2 = ctx.ok(need)
    if ok2.size == 0:
        return out
    rv = right.values[ok2]
    rn = right.null[ok2]
    l_false = ~left.values[ok2] & ~left.null[ok2]
    l_null = left.null[ok2]
    res_val = np.zeros(ok2.size, dtype=np.bool_)
    res_null = np.zeros(ok2.size, dtype=np.bool_)
    res_val[l_false] = rv[l_false]         # FALSE OR x = x
    res_null[l_false] = rn[l_false]
    # UNKNOWN OR TRUE = TRUE；UNKNOWN OR (FALSE|UNKNOWN) = UNKNOWN
    res_val[l_null] = True
    res_null[l_null] = rn[l_null] | ~rv[l_null]
    out.values[ok2] = res_val
    out.null[ok2] = res_null
    return out


_HANDLERS = {
    "add": _h_add,
    "div": _h_div,
    "to_int": _h_to_int,
    "eq": _h_eq,
    "gt": _h_gt,
    "is_null": _h_is_null,
    "not": _h_not,
    "if": _h_if,
    "coalesce": _h_coalesce,
    "and": _h_and,
    "or": _h_or,
}
