#!/usr/bin/env python3
"""
decompose.py — 依赖保持的 3NF 分解小工具（Python 3.11 + SQLite）

读取 schema.json（关系名、属性顺序、业务声明的函数依赖、CSV 路径）与 CSV 数据：
  1. 逐条用声明依赖检查输入数据；违例时给出决定列值与两条矛盾源行，拒绝发布；
  2. 计算最小覆盖（右部拆单属性、按属性顺序反复消除多余左部属性与冗余依赖，
     每次消除记录闭包依据；绝不把样例中偶然成立的关系当作证明）；
  3. 按相同左部合并成关系，删除被完整包含的关系；若无关系含原模式候选键，
     补入按属性数、再属性顺序最小的候选键关系；
  4. 输出 plan.json（方案 + 闭包依据 + 依赖推导）、decomposed.db（SQLite）、
     rebuild.sql（自然连接重建查询）、lineage.json（来源映射，不参与重建）；
  5. 实际执行重建查询并逐完整元组核对。

依赖是业务声明：本工具不从样例数据猜测任何额外约束。
范围约定：最多 6 个非空字符串列；集合语义，重复完整元组拒绝；NULL/空串不在范围内。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path

MAX_ATTRIBUTES = 6
IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class SpecError(Exception):
    """输入规格（schema.json / CSV 结构）不合法。"""


# ---------------------------------------------------------------- 基础结构

@dataclass
class FD:
    ident: str          # 稳定编号（如 m3）或所属关系名（如 rel_2）
    lhs: frozenset
    rhs: str


def fmt_fd(lhs_sorted, rhs):
    lhs = ", ".join(lhs_sorted) if lhs_sorted else "∅"
    return f"{lhs} -> {rhs}"


class Canon:
    """属性规范顺序：一切输出与决策都按 schema.json 声明的属性顺序，
    因此 CSV 行序与依赖列出顺序不影响规范方案。"""

    def __init__(self, attributes):
        self.attrs = list(attributes)
        self.idx = {a: i for i, a in enumerate(self.attrs)}

    def sorted_attrs(self, attr_set):
        return sorted(attr_set, key=self.idx.__getitem__)

    def attr_tuple_key(self, attr_set):
        return tuple(self.idx[a] for a in self.sorted_attrs(attr_set))

    def fd_sort_key(self, fd):
        return (self.attr_tuple_key(fd.lhs), self.idx[fd.rhs])

    def fd_label(self, fd):
        return fmt_fd(self.sorted_attrs(fd.lhs), fd.rhs)


def fd_label_with_ident(fd, canon):
    label = fmt_fd(canon.sorted_attrs(fd.lhs), fd.rhs)
    return f"{fd.ident}: {label}" if fd.ident else label


def closure_with_steps(seed, fds_sorted, canon):
    """在规范排序的 fds 上求 seed 的闭包，返回 (闭包集合, 逐步依据)。

    只使用给定依赖（业务声明或其等价变形），绝不查看数据。
    """
    clo = set(seed)
    steps = []
    changed = True
    while changed:
        changed = False
        for fd in fds_sorted:
            if fd.lhs <= clo and fd.rhs not in clo:
                clo.add(fd.rhs)
                steps.append({
                    "fd": fd_label_with_ident(fd, canon),
                    "adds": fd.rhs,
                    "closure": canon.sorted_attrs(clo),
                })
                changed = True
    return clo, steps


# ---------------------------------------------------------------- 读取输入

def load_spec(schema_path: Path):
    try:
        spec = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SpecError(f"无法读取 schema：{exc}") from exc

    relation = spec.get("relation", "wide_relation")
    if not isinstance(relation, str) or not IDENT_RE.fullmatch(relation):
        raise SpecError("schema.relation 必须是合法标识符字符串")

    attributes = spec.get("attributes")
    if not isinstance(attributes, list) or not attributes:
        raise SpecError("schema.attributes 必须是非空数组")
    if len(attributes) > MAX_ATTRIBUTES:
        raise SpecError(f"属性数 {len(attributes)} 超过上限 {MAX_ATTRIBUTES}")
    for a in attributes:
        if not isinstance(a, str) or not IDENT_RE.fullmatch(a):
            raise SpecError(f"属性名不合法：{a!r}")
    if len(set(attributes)) != len(attributes):
        raise SpecError("schema.attributes 存在重复属性")

    # 可选的 attribute_order：若给出，必须是 attributes 的排列，并作为规范属性顺序
    order = spec.get("attribute_order")
    if order is not None:
        if (not isinstance(order, list) or len(order) != len(attributes)
                or set(order) != set(attributes)):
            raise SpecError("schema.attribute_order 必须是 attributes 的一个排列")
        canon = Canon(order)
    else:
        canon = Canon(attributes)

    # 可选的 csv：缺省时由调用方用 --csv 指定，或回退到 schema 旁的 records.csv
    csv_file = spec.get("csv")
    if csv_file is not None and (not isinstance(csv_file, str) or not csv_file):
        raise SpecError("schema.csv 必须是非空字符串")

    deps = spec.get("dependencies")
    if not isinstance(deps, list):
        raise SpecError("schema.dependencies 必须是数组")

    attr_set = set(attributes)
    declared = []
    for i, d in enumerate(deps, 1):
        if not isinstance(d, dict):
            raise SpecError(f"第 {i} 条依赖不是对象")
        lhs, rhs = d.get("lhs"), d.get("rhs")
        if not isinstance(lhs, list) or not lhs:
            raise SpecError(f"第 {i} 条依赖 lhs 必须是非空数组（空左部不在范围内）")
        if not isinstance(rhs, list) or not rhs:
            raise SpecError(f"第 {i} 条依赖 rhs 必须是非空数组")
        for side, names in (("lhs", lhs), ("rhs", rhs)):
            for a in names:
                if a not in attr_set:
                    raise SpecError(f"第 {i} 条依赖 {side} 含未知属性 {a!r}")
            if len(set(names)) != len(names):
                raise SpecError(f"第 {i} 条依赖 {side} 含重复属性")
        declared.append((frozenset(lhs), tuple(canon.sorted_attrs(rhs))))

    # 规范化排序并重新编号：依赖列出顺序不影响规范方案
    declared.sort(key=lambda t: (canon.attr_tuple_key(t[0]),
                                 tuple(canon.idx[a] for a in t[1])))
    declared_fds = [
        {"id": f"FD{k}", "lhs": canon.sorted_attrs(lhs), "rhs": list(rhs)}
        for k, (lhs, rhs) in enumerate(declared, 1)
    ]
    return {
        "relation": relation,
        "attributes": attributes,
        "csv": csv_file,
        "canon": canon,
        "declared": declared_fds,
    }


def load_csv(csv_path: Path, canon: Canon):
    try:
        f = csv_path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise SpecError(f"无法读取 CSV：{exc}") from exc
    with f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise SpecError("CSV 为空（缺少表头）") from None
        if len(set(header)) != len(header):
            raise SpecError("CSV 表头含重复列")
        if set(header) != set(canon.attrs):
            raise SpecError(f"CSV 表头 {header} 与 schema 属性 {canon.attrs} 不一致")
        positions = [canon.idx[h] for h in header]
        rows = []
        for rowno, record in enumerate(reader, 1):  # 逻辑行号：数据行从 1 起，不计表头
            if len(record) != len(header):
                raise SpecError(f"第 {rowno} 行列数 {len(record)} 与表头 {len(header)} 不符")
            row = [None] * len(canon.attrs)
            for value, pos in zip(record, positions):
                if value == "":
                    raise SpecError(
                        f"第 {rowno} 行属性 {canon.attrs[pos]!r} 为空：NULL/空串不在本工具范围内")
                row[pos] = value
            rows.append(tuple(row))
    seen = {}
    for rowno, row in enumerate(rows, 1):
        if row in seen:
            raise SpecError(
                f"重复完整元组：第 {rowno} 行与第 {seen[row]} 行完全相同（集合语义，拒绝）")
        seen[row] = rowno
    if not rows:
        raise SpecError("CSV 没有任何数据行")
    return rows


# ---------------------------------------------------------------- 输入检查

def validate_dependencies(rows, declared, canon):
    """逐条用声明依赖检查输入；返回违例列表（空列表表示通过）。

    每个违例给出决定列值与两条矛盾源行（含逻辑行号与完整行内容）。
    """
    violations = []
    for d in declared:
        lhs_attrs = d["lhs"]
        lhs_pos = [canon.idx[a] for a in lhs_attrs]
        groups = {}
        for rowno, row in enumerate(rows, 1):
            key = tuple(row[p] for p in lhs_pos)
            groups.setdefault(key, []).append((rowno, row))
        for rhs_attr in d["rhs"]:
            if rhs_attr in d["lhs"]:
                continue  # 平凡部分恒成立
            rp = canon.idx[rhs_attr]
            for key in sorted(groups):
                members = groups[key]
                first_no, first_row = members[0]
                for other_no, other_row in members[1:]:
                    if other_row[rp] != first_row[rp]:
                        violations.append({
                            "fd": f'{d["id"]}: {fmt_fd(lhs_attrs, rhs_attr)}',
                            "determinant": dict(zip(lhs_attrs, key)),
                            "conflicting_attribute": rhs_attr,
                            "row_a": {"row": first_no,
                                      "values": dict(zip(canon.attrs, first_row))},
                            "row_b": {"row": other_no,
                                      "values": dict(zip(canon.attrs, other_row))},
                        })
                        break
    return violations


# ---------------------------------------------------------------- 最小覆盖

def minimal_cover(declared, canon):
    """由声明依赖计算等价最小覆盖；每次消除都记录闭包依据。"""
    steps = []
    fds = []

    # 1) 右部拆成单属性；丢弃平凡依赖
    for d in declared:
        produced = []
        for b in d["rhs"]:
            if b in d["lhs"]:
                steps.append({
                    "action": "drop_trivial",
                    "from": d["id"],
                    "fd": fmt_fd(d["lhs"], b),
                    "reason": "右部属性已在左部，平凡成立",
                })
            else:
                fds.append(FD("", frozenset(d["lhs"]), b))
                produced.append(fmt_fd(d["lhs"], b))
        steps.append({
            "action": "split_rhs",
            "from": d["id"],
            "produced": produced or ["（全部为平凡依赖，无产出）"],
        })

    # 2) 规范排序、去重后赋予稳定编号 m1..mk
    fds.sort(key=canon.fd_sort_key)
    deduped, seen = [], set()
    for fd in fds:
        key = (fd.lhs, fd.rhs)
        if key in seen:
            steps.append({
                "action": "drop_duplicate",
                "fd": canon.fd_label(fd),
                "reason": "拆分后与保留的依赖完全相同",
            })
        else:
            seen.add(key)
            deduped.append(fd)
    fds = deduped
    for k, fd in enumerate(fds, 1):
        fd.ident = f"m{k}"

    # 3) 反复消除多余左部属性与冗余依赖，直到不动点
    changed = True
    while changed:
        changed = False
        fds.sort(key=canon.fd_sort_key)

        # 3a) 多余左部属性：按属性顺序尝试删除，右部仍落在剩余左部闭包中则可删
        for fd in list(fds):
            for a in canon.sorted_attrs(fd.lhs):
                if a not in fd.lhs or len(fd.lhs) <= 1:
                    continue
                rest = fd.lhs - {a}
                clo, csteps = closure_with_steps(rest, fds, canon)
                if fd.rhs in clo:
                    steps.append({
                        "action": "remove_lhs_attribute",
                        "fd": fd_label_with_ident(fd, canon),
                        "removed_attribute": a,
                        "remaining_lhs": canon.sorted_attrs(rest),
                        "justification": {
                            "closure_of": canon.sorted_attrs(rest),
                            "target": fd.rhs,
                            "steps": csteps,
                        },
                    })
                    fd.lhs = rest
                    changed = True

        # 3b) 左部化简后可能出现新的完全相同依赖
        seen, kept = {}, []
        for fd in sorted(fds, key=canon.fd_sort_key):
            key = (fd.lhs, fd.rhs)
            if key in seen:
                steps.append({
                    "action": "drop_duplicate",
                    "fd": fd_label_with_ident(fd, canon),
                    "kept": fd_label_with_ident(seen[key], canon),
                    "reason": "左部化简后与已有依赖相同",
                })
                changed = True
            else:
                seen[key] = fd
                kept.append(fd)
        fds = kept

        # 3c) 冗余依赖：右部仍落在左部对其余依赖的闭包中则可删
        for fd in list(fds):
            others = [g for g in fds if g is not fd]
            clo, csteps = closure_with_steps(fd.lhs, others, canon)
            if fd.rhs in clo:
                steps.append({
                    "action": "remove_redundant_fd",
                    "fd": fd_label_with_ident(fd, canon),
                    "justification": {
                        "closure_of": canon.sorted_attrs(fd.lhs),
                        "target": fd.rhs,
                        "steps": csteps,
                    },
                })
                fds.remove(fd)
                changed = True

    fds.sort(key=canon.fd_sort_key)
    return fds, steps


# ---------------------------------------------------------------- 3NF 合成

def candidate_keys(universe, cover, canon):
    """universe 的全部候选键，按（属性数、属性顺序）升序返回。"""
    keys = []
    attrs = canon.sorted_attrs(universe)
    for size in range(1, len(attrs) + 1):
        for combo in combinations(attrs, size):
            cand = frozenset(combo)
            if any(k <= cand for k in keys):
                continue
            clo, _ = closure_with_steps(cand, cover, canon)
            if clo >= set(universe):
                keys.append(cand)
    return keys


def synthesize(cover, canon):
    """按相同左部合并属性形成关系；删除被其他关系完整包含的关系。"""
    groups = {}
    for fd in cover:
        groups.setdefault(fd.lhs, []).append(fd)
    relations = []
    for lhs in sorted(groups, key=canon.attr_tuple_key):
        attrs = set(lhs)
        for fd in groups[lhs]:
            attrs.add(fd.rhs)
        relations.append(frozenset(attrs))
    relations = sorted(set(relations), key=canon.attr_tuple_key)

    removed, kept = [], []
    for r in relations:
        container = next((s for s in relations if s != r and r <= s), None)
        if container is not None:
            removed.append({
                "relation": canon.sorted_attrs(r),
                "contained_in": canon.sorted_attrs(container),
            })
        else:
            kept.append(r)
    return kept, removed


def projected_dependencies(rel_attrs, cover, canon):
    """投影到关系上的极小左部非平凡依赖：X->A，X⊆rel，A∈rel，A∉X，A∈X⁺(cover)。"""
    deps = []
    for a in canon.sorted_attrs(rel_attrs):
        determinants = []
        others = [x for x in canon.sorted_attrs(rel_attrs) if x != a]
        for size in range(1, len(others) + 1):
            for combo in combinations(others, size):
                x = frozenset(combo)
                if any(d <= x for d in determinants):
                    continue
                clo, _ = closure_with_steps(x, cover, canon)
                if a in clo:
                    determinants.append(x)
                    deps.append(FD("", x, a))
    return deps


def derive_declared(declared, relation_deps, canon):
    """每条原声明依赖如何由投影到各关系的依赖闭包共同推出。"""
    g = []
    for rel_name, deps in relation_deps:
        for fd in deps:
            g.append(FD(rel_name, fd.lhs, fd.rhs))
    g.sort(key=canon.fd_sort_key)

    derivations, all_ok = [], True
    for d in declared:
        per_rhs = []
        for b in d["rhs"]:
            if b in d["lhs"]:
                per_rhs.append({
                    "rhs": b, "derived": True, "trivial": True,
                    "note": "右部属性已在左部，平凡成立",
                })
                continue
            clo, csteps = closure_with_steps(frozenset(d["lhs"]), g, canon)
            ok = b in clo
            all_ok = all_ok and ok
            per_rhs.append({
                "rhs": b,
                "derived": ok,
                "closure_of": d["lhs"],
                "steps": csteps,
            })
        derivations.append({
            "fd": f'{d["id"]}: {fmt_fd(d["lhs"], ", ".join(d["rhs"]))}',
            "derived": all(p["derived"] for p in per_rhs),
            "per_rhs": per_rhs,
        })
    return derivations, all_ok


def compute_plan(spec):
    canon = spec["canon"]
    cover, mc_steps = minimal_cover(spec["declared"], canon)

    relations, removed = synthesize(cover, canon)
    schema_keys = candidate_keys(set(canon.attrs), cover, canon)

    added_key = None
    if not any(k <= r for k in schema_keys for r in relations):
        added_key = schema_keys[0]  # 属性数、再属性顺序最小
        relations = relations + [added_key]
    relations = sorted(relations, key=canon.attr_tuple_key)

    infos, relation_deps = [], []
    for i, r in enumerate(relations, 1):
        keys = candidate_keys(r, cover, canon)
        deps = projected_dependencies(r, cover, canon)
        name = f"rel_{i}"
        infos.append({
            "name": name,
            "attributes": canon.sorted_attrs(r),
            "keys": [canon.sorted_attrs(k) for k in keys],
            "primary_key": canon.sorted_attrs(keys[0]),
            "projected_dependencies": [
                {"lhs": canon.sorted_attrs(d.lhs), "rhs": d.rhs} for d in deps
            ],
            "origin": "candidate_key" if (added_key is not None and r == added_key)
                      else "synthesis",
        })
        relation_deps.append((name, deps))

    derivations, all_ok = derive_declared(spec["declared"], relation_deps, canon)
    if not all_ok:
        raise RuntimeError("内部错误：合成结果未能保持全部声明依赖")

    plan = {
        "tool": "decompose.py — 依赖保持的 3NF 分解",
        "method": {
            "construction": "dependency_preserving_3nf_synthesis",
            "note": ("无损连接由构造保证（依赖保持且某关系含原模式候选键），"
                     "并非因为当前数据连接回来恰好相等；对当前数据的重建核对只是 sanity check，"
                     "不能据此宣称未来所有合法关系都无损。"),
            "not_goals": ["不搜索 BCNF 或最小表数分解",
                          "不做在线迁移、兼容写视图"],
        },
        "attributes": canon.attrs,
        "declared_dependencies": spec["declared"],
        "minimal_cover": {
            "steps": mc_steps,
            "cover": [
                {"id": fd.ident, "lhs": canon.sorted_attrs(fd.lhs), "rhs": fd.rhs}
                for fd in cover
            ],
        },
        "schema_candidate_keys": [canon.sorted_attrs(k) for k in schema_keys],
        "relations": infos,
        "containment_removed": removed,
        "key_relation_added": canon.sorted_attrs(added_key) if added_key else None,
        "dependency_derivations": derivations,
        "dependency_preserving": all_ok,
    }
    return plan, infos


# ---------------------------------------------------------------- 落库与重建

def build_database(db_path: Path, infos, rows, canon):
    """按方案从真实输入投影去重并载入各表；库中只有分解后的关系，不存宽表。"""
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        for info in infos:
            name, attrs = info["name"], info["attributes"]
            cols = ", ".join(f'"{a}" TEXT NOT NULL' for a in attrs)
            pk = ", ".join(f'"{a}"' for a in info["primary_key"])
            uniques = [
                "UNIQUE (" + ", ".join(f'"{a}"' for a in key) + ")"
                for key in info["keys"][1:]
            ]
            ddl = f'CREATE TABLE "{name}" ({cols}, PRIMARY KEY ({pk})'
            if uniques:
                ddl += ", " + ", ".join(uniques)
            ddl += ")"
            cur.execute(ddl)
            projected = sorted({tuple(row[canon.idx[a]] for a in attrs) for row in rows})
            placeholders = ", ".join("?" * len(attrs))
            cur.executemany(f'INSERT INTO "{name}" VALUES ({placeholders})', projected)
            info["row_count"] = len(projected)
        conn.commit()
    finally:
        conn.close()


def build_lineage(infos, rows, canon):
    """来源映射：每个投影元组对应的原逻辑行号；仅供追溯，不参与重建。"""
    lineage = {}
    for info in infos:
        attrs = info["attributes"]
        pos = [canon.idx[a] for a in attrs]
        mapping = {}
        for rowno, row in enumerate(rows, 1):
            t = tuple(row[p] for p in pos)
            mapping.setdefault(t, []).append(rowno)
        lineage[info["name"]] = [
            {"tuple": dict(zip(attrs, t)), "source_rows": srcs}
            for t, srcs in sorted(mapping.items())
        ]
    return lineage


def rebuild_sql(infos, canon, relation_name):
    select_cols = ", ".join(canon.attrs)
    joins = "\n  NATURAL JOIN ".join(f'"{i["name"]}"' for i in infos)
    order = ", ".join(canon.attrs)
    core = f"SELECT {select_cols}\nFROM {joins}\nORDER BY {order}"
    text = (
        f"-- 自然连接重建原始宽关系 {relation_name}\n"
        f"-- 无损性来自依赖保持的 3NF 构造（某关系含原模式候选键），而非当前数据恰好如此。\n"
        + core + ";\n"
    )
    return text, core


def verify_rebuild(db_path: Path, core_sql, rows):
    conn = sqlite3.connect(str(db_path))
    try:
        got = [tuple(r) for r in conn.execute(core_sql)]
    finally:
        conn.close()
    ok = sorted(got) == sorted(rows)
    return ok, got


# ---------------------------------------------------------------- 主流程

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="依赖保持的 3NF 分解工具：schema.json + CSV → 方案 + SQLite + 重建查询")
    parser.add_argument("schema", type=Path, help="schema.json 路径")
    parser.add_argument("--csv", type=Path, default=None,
                        help="覆盖 schema 中声明的 CSV 路径")
    parser.add_argument("--out", type=Path, default=None,
                        help="输出目录（默认 <schema目录>/out）")
    args = parser.parse_args(argv)

    try:
        spec = load_spec(args.schema)
    except SpecError as exc:
        print(f"规格错误：{exc}", file=sys.stderr)
        return 1
    canon = spec["canon"]

    if args.csv:
        csv_path = args.csv
    elif spec["csv"]:
        csv_path = args.schema.parent / spec["csv"]
    else:
        csv_path = args.schema.parent / "records.csv"  # 缺省：schema 旁的 records.csv
    if not csv_path.is_absolute():
        csv_path = Path.cwd() / csv_path
    out_dir = args.out if args.out else args.schema.parent / "out"

    try:
        rows = load_csv(csv_path, canon)
    except SpecError as exc:
        print(f"输入错误：{exc}", file=sys.stderr)
        return 1

    # 先按声明依赖逐条检查输入；违例则拒绝发布（不产生任何输出）
    violations = validate_dependencies(rows, spec["declared"], canon)
    if violations:
        print("输入违反业务声明的函数依赖，拒绝发布：", file=sys.stderr)
        for i, v in enumerate(violations, 1):
            det = ", ".join(f"{k} = {val}" for k, val in v["determinant"].items())
            a, b = v["row_a"], v["row_b"]
            attr = v["conflicting_attribute"]
            print(f"[违例 {i}] {v['fd']}", file=sys.stderr)
            print(f"  决定列值: {det}", file=sys.stderr)
            print(f"  矛盾源行: 第 {a['row']} 行 与 第 {b['row']} 行"
                  f"（{attr}: {a['values'][attr]!r} != {b['values'][attr]!r}）",
                  file=sys.stderr)
            print(f"    第 {a['row']} 行: {json.dumps(a['values'], ensure_ascii=False)}",
                  file=sys.stderr)
            print(f"    第 {b['row']} 行: {json.dumps(b['values'], ensure_ascii=False)}",
                  file=sys.stderr)
        return 1

    plan, infos = compute_plan(spec)

    out_dir.mkdir(parents=True, exist_ok=True)
    db_path = out_dir / "decomposed.db"
    build_database(db_path, infos, rows, canon)

    lineage = build_lineage(infos, rows, canon)
    (out_dir / "lineage.json").write_text(json.dumps({
        "description": "来源映射：每个投影元组对应的原逻辑行号（1 起，不计表头）；仅供追溯，不参与重建。",
        "relations": lineage,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    sql_text, core_sql = rebuild_sql(infos, canon, spec["relation"])
    (out_dir / "rebuild.sql").write_text(sql_text, encoding="utf-8")

    ok, got = verify_rebuild(db_path, core_sql, rows)
    plan["source"] = {
        "schema": args.schema.name, "csv": csv_path.name,
        "relation": spec["relation"], "rows": len(rows),
    }
    plan["validation"] = {
        "status": "ok",
        "checked_dependencies": len(spec["declared"]),
        "rows": len(rows),
    }
    plan["rebuild"] = {
        "sql_file": "rebuild.sql",
        "verified": ok,
        "tuples_original": len(rows),
        "tuples_rebuilt": len(got),
    }
    (out_dir / "plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if not ok:
        missing = sorted(set(rows) - set(got))
        extra = sorted(set(got) - set(rows))
        print("重建核对失败：自然连接结果与原关系不一致", file=sys.stderr)
        print(f"  原关系有而重建缺 {len(missing)} 条，如 {missing[:3]}", file=sys.stderr)
        print(f"  重建多出 {len(extra)} 条，如 {extra[:3]}", file=sys.stderr)
        return 2

    # 人类可读摘要
    print("== 输入检查 ==")
    print(f"已按 {len(spec['declared'])} 条声明依赖检查 {len(rows)} 行：通过")
    print("== 最小覆盖 ==")
    for fd in plan["minimal_cover"]["cover"]:
        print(f"  {fd['id']}: {fmt_fd(fd['lhs'], fd['rhs'])}")
    print("== 关系 ==")
    for info in infos:
        keys = "; ".join("(" + ", ".join(k) + ")" for k in info["keys"])
        print(f"  {info['name']}({', '.join(info['attributes'])})"
              f"  键: {keys}  行数: {info['row_count']}  来源: {info['origin']}")
    if plan["key_relation_added"]:
        print(f"  （补入候选键关系: {', '.join(plan['key_relation_added'])}）")
    print("== 依赖推导 ==")
    for d in plan["dependency_derivations"]:
        print(f"  {d['fd']}  ✓ 可由各关系投影依赖的闭包共同推出")
    print("== 重建核对 ==")
    join_names = " ⋈ ".join(i["name"] for i in infos)
    print(f"  {join_names} → {len(got)} 个元组，与原关系逐完整元组一致 ✓")
    print(f"输出目录 {out_dir}: plan.json, decomposed.db, rebuild.sql, lineage.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
