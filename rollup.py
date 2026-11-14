"""预聚合表的安全复用：plan / run / 独立对账。

范围：单事实表 + 少量预聚合候选。查询为固定 JSON 结构：
  {"id": ..., "group_by": [维度...], "where": {维度: [允许值...可含 null]},
   "measures": ["count", "amount_sum", "amount_avg"]}
不解析任意 SQL。维度 NULL 值作为独立组；允许值集合中的 null 匹配 NULL。
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

QUERY_MEASURES = ("count", "amount_sum", "amount_avg")

# 回答每个查询度量所需的候选逻辑度量。
# amount_sum 需要 amount_count：重聚合用 CASE WHEN SUM(n_amount)>0 THEN SUM(s_amount) END，
# 以保全“全 NULL 组 SUM 为 NULL”的口径；amount_avg 必须用 总SUM/总非NULLCOUNT 重算，
# 只有 avg_amount 而没有权重的候选不够（不能按组数平均）。
NEEDED_VIEW_MEASURES = {
    "count": ("count",),
    "amount_sum": ("amount_sum", "amount_count"),
    "amount_avg": ("amount_sum", "amount_count"),
}


def _fmt_vals(vals) -> str:
    return "[" + ", ".join(
        "null" if v is None else json.dumps(v, ensure_ascii=False) for v in vals
    ) + "]"


# ---------------------------------------------------------------- 模型与校验

@dataclass(frozen=True)
class View:
    name: str
    dimensions: tuple
    measures: dict          # 逻辑度量 -> 候选表列名
    predicate: dict         # 维度 -> 已应用的允许值集合（可含 None）
    snapshot: str


@dataclass(frozen=True)
class Model:
    fact: str
    snapshot: str
    dimensions: tuple
    views: tuple


def load_model(path) -> Model:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    dims = tuple(raw["dimensions"])
    views = []
    for v in raw.get("views", []):
        unknown = (set(v["dimensions"]) | set(v.get("predicate", {}))) - set(dims)
        if unknown:
            raise ValueError(f"候选 {v['name']} 引用未声明维度 {sorted(unknown)}")
        views.append(View(
            name=v["name"],
            dimensions=tuple(v["dimensions"]),
            measures=dict(v["measures"]),
            predicate={k: tuple(vv) for k, vv in v.get("predicate", {}).items()},
            snapshot=v["snapshot"],
        ))
    return Model(fact=raw["fact"], snapshot=raw["snapshot"],
                 dimensions=dims, views=tuple(views))


def validate_query(model: Model, q: dict) -> None:
    for key in ("id", "group_by", "where", "measures"):
        if key not in q:
            raise ValueError(f"查询缺少字段 {key!r}")
    gb, where, measures = q["group_by"], q["where"], q["measures"]
    if not isinstance(gb, list) or len(set(gb)) != len(gb):
        raise ValueError("group_by 必须为不重复的维度列表")
    unknown = (set(gb) | set(where)) - set(model.dimensions)
    if unknown:
        raise ValueError(f"查询引用未声明维度 {sorted(unknown)}")
    if not isinstance(measures, list) or not measures:
        raise ValueError("measures 必须为非空列表")
    bad = [m for m in measures if m not in QUERY_MEASURES]
    if bad:
        raise ValueError(f"不支持的度量 {bad}，仅支持 {list(QUERY_MEASURES)}")
    for d, vals in where.items():
        if not isinstance(vals, list):
            raise ValueError(f"where.{d} 必须为允许值列表")
        for v in vals:
            if isinstance(v, (dict, list)):
                raise ValueError(f"where.{d} 含非法值 {v!r}")


# ---------------------------------------------------------------- 候选判定

def check_view(model: Model, view: View, q: dict):
    """返回 (残余过滤 dict, None) 表示可用；否则 (None, 拒绝原因)。"""
    if view.snapshot != model.snapshot:
        return None, (f"snapshot 不匹配：候选为 {view.snapshot!r}，"
                      f"查询事实快照为 {model.snapshot!r}（旧快照，拒绝）")
    for m in q["measures"]:
        missing = [nm for nm in NEEDED_VIEW_MEASURES[m] if nm not in view.measures]
        if missing:
            if set(view.measures) == {"amount_avg"}:
                return None, ("只有 avg_amount 而没有权重（amount_sum/amount_count），"
                              "AVG 无法按 总SUM/总非NULLCOUNT 重算，拒绝")
            return None, f"缺少度量 {missing}，无法回答 {m}"
    missing_g = [d for d in q["group_by"] if d not in view.dimensions]
    if missing_g:
        return None, (f"分组维度 {missing_g} 不能由候选维度 "
                      f"{sorted(view.dimensions)} 表示")
    residual = {}
    for dim, allowed in q["where"].items():
        if dim in view.predicate:
            pred = set(view.predicate[dim])
            if not set(allowed) <= pred:
                return None, (f"候选行域 {dim}∈{_fmt_vals(sorted(pred, key=repr))} "
                              f"不覆盖查询所需 {dim}∈{_fmt_vals(allowed)}")
            if set(allowed) == pred:
                continue  # 查询条件已由候选谓词完全满足，无需该列
            # 严格子集：仍需残余过滤，要求候选带该列
        if dim not in view.dimensions:
            return None, (f"过滤维度 {dim!r} 既未被候选谓词完全满足，"
                          f"也不在候选维度 {sorted(view.dimensions)} 中")
        residual[dim] = allowed
    for dim, pred in view.predicate.items():
        if dim not in q["where"]:
            return None, (f"候选已按 {dim}∈{_fmt_vals(sorted(pred, key=repr))} 预过滤，"
                          f"而查询未限制该维度（需要全部取值），行域不覆盖")
    return residual, None


# ---------------------------------------------------------------- SQL 生成

def _in_cond(dim: str, allowed: list):
    """允许值集合 → 参数化条件；集合中的 None 匹配 NULL。"""
    if not allowed:
        return "1=0", []  # 空允许集：不匹配任何行
    non_null = [v for v in allowed if v is not None]
    has_null = len(non_null) != len(allowed)
    parts, params = [], []
    if non_null:
        parts.append(f'"{dim}" IN ({", ".join("?" * len(non_null))})')
        params.extend(non_null)
    if has_null:
        parts.append(f'"{dim}" IS NULL')
    if len(parts) == 1:
        return parts[0], params
    return "(" + " OR ".join(parts) + ")", params


def _build_where(conditions: dict):
    clauses, params = [], []
    for dim, allowed in conditions.items():
        cond, ps = _in_cond(dim, allowed)
        clauses.append(cond)
        params.extend(ps)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _measure_selects(measures, count_col, n_amount_col, s_amount_col):
    """返回 (select 片段, 重聚合表达式说明)。三个列名表达式适用于候选或 fact。"""
    selects, reagg = [], {}
    if "count" in measures:
        expr = f"COALESCE(SUM({count_col}), 0)"
        reagg["count"] = expr
        selects.append(expr + ' AS "count"')
    if "amount_sum" in measures:
        expr = (f"CASE WHEN SUM({n_amount_col}) > 0 "
                f"THEN SUM({s_amount_col}) END")
        reagg["amount_sum"] = expr
        selects.append(expr + ' AS "amount_sum"')
    if "amount_avg" in measures:
        num, den = f"SUM({s_amount_col})", f"SUM({n_amount_col})"
        reagg["amount_avg"] = f"{num} / {den}（零分母 → NULL）"
        selects.append(num + ' AS "amount_avg_num"')
        selects.append(den + ' AS "amount_avg_den"')
    return selects, reagg


def build_view_sql(view: View, q: dict, residual: dict):
    # check_view 已保证所需逻辑度量存在，这里直接取列名
    m = view.measures
    selects, reagg = _measure_selects(
        q["measures"], f'"{m["count"]}"' if "count" in m else "NULL",
        f'"{m["amount_count"]}"' if "amount_count" in m else "NULL",
        f'"{m["amount_sum"]}"' if "amount_sum" in m else "NULL")
    cols = [f'"{d}"' for d in q["group_by"]] + selects
    where, params = _build_where(residual)
    sql = f'SELECT {", ".join(cols)} FROM "{view.name}"' + where
    if q["group_by"]:
        sql += " GROUP BY " + ", ".join(f'"{d}"' for d in q["group_by"])
    return sql, params, reagg


def build_fact_sql(model: Model, q: dict):
    # fact 明细上：count 用 COUNT(*)，非NULL计数用 COUNT(amount)
    selects, reagg = [], {}
    if "count" in q["measures"]:
        reagg["count"] = "COUNT(*)"
        selects.append('COUNT(*) AS "count"')
    if "amount_sum" in q["measures"]:
        expr = "CASE WHEN COUNT(amount) > 0 THEN SUM(amount) END"
        reagg["amount_sum"] = expr
        selects.append(expr + ' AS "amount_sum"')
    if "amount_avg" in q["measures"]:
        reagg["amount_avg"] = "SUM(amount) / COUNT(amount)（零分母 → NULL）"
        selects.append('SUM(amount) AS "amount_avg_num"')
        selects.append('COUNT(amount) AS "amount_avg_den"')
    cols = [f'"{d}"' for d in q["group_by"]] + selects
    where, params = _build_where(q["where"])
    sql = f'SELECT {", ".join(cols)} FROM "{model.fact}"' + where
    if q["group_by"]:
        sql += " GROUP BY " + ", ".join(f'"{d}"' for d in q["group_by"])
    return sql, params, reagg


# ---------------------------------------------------------------- 计划

@dataclass
class Verdict:
    name: str
    usable: bool
    reason: str
    row_count: int | None = None


@dataclass
class Plan:
    query: dict
    verdicts: list
    chosen: str                 # 实际使用的表（候选名或 fact）
    fallback: bool
    fallback_reason: str | None
    residual_where: dict
    reaggregation: dict
    sql: str
    params: list


class _ReadRecorder:
    """通过 sqlite authorizer 记录实际读取的表。"""

    def __init__(self):
        self.tables = set()

    def __call__(self, action, arg1, arg2, _db, _src):
        if action == sqlite3.SQLITE_READ and arg1:
            self.tables.add(arg1)
        return sqlite3.SQLITE_OK


def execute_recording(conn, sql, params=()):
    rec = _ReadRecorder()
    conn.set_authorizer(rec)
    try:
        cur = conn.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    finally:
        conn.set_authorizer(None)
    return cols, rows, rec.tables


def plan(model: Model, q: dict, conn) -> Plan:
    """为查询选择候选：全部候选给出可用/拒绝原因；可用者按实际行数最少、
    再按表名字节序选一个；无可用候选时明确回退 fact。"""
    validate_query(model, q)
    verdicts, usable = [], []
    for v in model.views:
        residual, reason = check_view(model, v, q)
        if residual is None:
            verdicts.append(Verdict(v.name, False, reason))
        else:
            _, rows, _ = execute_recording(conn, f'SELECT COUNT(*) FROM "{v.name}"')
            rc = rows[0][0]
            verdicts.append(Verdict(v.name, True, "可用", rc))
            usable.append((rc, v.name.encode("utf-8"), v, residual))
    if usable:
        usable.sort(key=lambda t: (t[0], t[1]))
        rc, _, view, residual = usable[0]
        sql, params, reagg = build_view_sql(view, q, residual)
        return Plan(q, verdicts, view.name, False, None, residual, reagg, sql, params)
    reasons = "; ".join(f"{vd.name}: {vd.reason}" for vd in verdicts)
    sql, params, reagg = build_fact_sql(model, q)
    return Plan(q, verdicts, model.fact, True,
                f"无可用候选（{reasons}），回退 fact 明细聚合",
                dict(q["where"]), reagg, sql, params)


# ---------------------------------------------------------------- 执行

def _assemble(cols, rows, q):
    entries = []
    for r in rows:
        rec = dict(zip(cols, r))
        e = {d: rec[d] for d in q["group_by"]}
        if "count" in q["measures"]:
            e["count"] = rec["count"]
        if "amount_sum" in q["measures"]:
            e["amount_sum"] = rec["amount_sum"]
        if "amount_avg" in q["measures"]:
            num, den = rec["amount_avg_num"], rec["amount_avg_den"]
            e["amount_avg"] = Fraction(num, den) if den else None
        entries.append(e)
    return entries


def run(conn, model: Model, q: dict, p: Plan):
    """实际执行计划生成的参数化 SQL；返回 (结果行, 实际读取的表集合)。"""
    cols, rows, tables = execute_recording(conn, p.sql, p.params)
    return _assemble(cols, rows, q), tables


# ---------------------------------------------------------------- 独立对账

def direct_fact(conn, model: Model, q: dict):
    """独立路径：直接读 fact 明细在 Python 侧分组聚合，用于逐组对账。"""
    dims = list(model.dimensions)
    sql = (f"SELECT {', '.join(chr(34) + d + chr(34) for d in dims)}, "
           f'"amount" FROM "{model.fact}"')
    cols, rows, tables = execute_recording(conn, sql)
    groups = {}
    for row in rows:
        rec = dict(zip(cols, row))
        if not all(rec[d] in vals for d, vals in q["where"].items()):
            continue
        key = tuple(rec[d] for d in q["group_by"])
        n, na, s = groups.get(key, (0, 0, 0))
        if rec["amount"] is not None:
            na, s = na + 1, s + rec["amount"]
        groups[key] = (n + 1, na, s)
    entries = []
    for key, (n, na, s) in groups.items():
        e = dict(zip(q["group_by"], key))
        if "count" in q["measures"]:
            e["count"] = n
        if "amount_sum" in q["measures"]:
            e["amount_sum"] = s if na > 0 else None
        if "amount_avg" in q["measures"]:
            e["amount_avg"] = Fraction(s, na) if na > 0 else None
        entries.append(e)
    if not q["group_by"] and not entries:
        e = {}
        if "count" in q["measures"]:
            e["count"] = 0
        if "amount_sum" in q["measures"]:
            e["amount_sum"] = None
        if "amount_avg" in q["measures"]:
            e["amount_avg"] = None
        entries.append(e)
    return entries, tables


def reconcile(a, b, q):
    """逐组对账：组集合、count、amount_sum（含 NULL）、amount_avg（精确分数）。"""
    def keyed(entries):
        return {tuple(e[d] for d in q["group_by"]): e for e in entries}
    ka, kb = keyed(a), keyed(b)
    diffs = []
    for key in sorted(ka.keys() | kb.keys(), key=repr):
        if key not in ka:
            diffs.append(f"组 {key}: 计划结果缺失")
            continue
        if key not in kb:
            diffs.append(f"组 {key}: 直接 fact 结果缺失")
            continue
        for m in q["measures"]:
            va, vb = ka[key][m], kb[key][m]
            if va != vb or type(va) is not type(vb):
                diffs.append(f"组 {key} 度量 {m}: 计划={va!r} 直接={vb!r}")
    return not diffs, diffs
