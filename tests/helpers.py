"""测试公共夹具：在临时目录里构造独立的 sources/plans/JSONL。"""

from __future__ import annotations

import json
from pathlib import Path

from etl.catalog import Catalog
from etl.planner import Planner
from etl.runner import run_plan

SCHEMA = {
    "id": "int",
    "region": "text",
    "tick": "int",
    "amount": "int?",
}
ORDER = [
    {"field": "region", "direction": "asc", "nulls": "first"},
    {"field": "tick", "direction": "asc", "nulls": "first"},
]


def write_source(base: Path, name: str, rows: list[dict],
                 order: list[dict] | None = None,
                 schema: dict | None = None,
                 data_name: str | None = None) -> str:
    data_name = data_name or name
    jsonl = base / f"{data_name}.jsonl"
    with jsonl.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    sources_path = base / "sources.json"
    declarations = {}
    if sources_path.exists():
        declarations = json.loads(sources_path.read_text(encoding="utf-8"))
    declarations[name] = {
        "path": str(jsonl),
        "schema": schema or SCHEMA,
        "order": ORDER if order is None else order,
    }
    sources_path.write_text(
        json.dumps(declarations, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(sources_path)


def write_plan(base: Path, plan: dict) -> str:
    plans_path = base / "plans.json"
    plans = []
    if plans_path.exists():
        plans = json.loads(plans_path.read_text(encoding="utf-8"))
    plans.append(plan)
    plans_path.write_text(
        json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return str(plans_path)


def execute(tmp_path: Path, sources_path: str, plan: dict):
    plans_path = write_plan(tmp_path, plan)
    catalog = Catalog(sources_path)
    planned = Planner(catalog).plan(plan)
    output_rows, report = run_plan(catalog, planned["executable_plan"])
    return planned, output_rows, report


def dict_group(rows: list[dict], keys: list[str], sum_field: str) -> list[dict]:
    """独立的、不依赖输入顺序的字典聚合，作为对照基准。"""
    groups: dict[tuple, dict] = {}
    first_seen: list[tuple] = []
    for row in rows:
        key = tuple(row.get(k) for k in keys)
        if key not in groups:
            groups[key] = {"count": 0, "sum": None}
            first_seen.append(key)
        groups[key]["count"] += 1
        value = row.get(sum_field)
        if value is not None:
            groups[key]["sum"] = (
                value if groups[key]["sum"] is None else groups[key]["sum"] + value
            )
    # ASC + NULLS FIRST 的对照排序键：None 在最前，其余按值升序。
    expected = []
    for key in sorted(first_seen, key=lambda k: tuple((v is not None, v) for v in k)):
        row_out = dict(zip(keys, key))
        row_out["count"] = groups[key]["count"]
        row_out["sum"] = groups[key]["sum"]
        expected.append(row_out)
    return expected
