"""命令行入口：

    python -m etl plan   [--sources ...] [--plans ...] [--out artifacts]
    python -m etl run    [--sources ...] [--plans ...] [--out artifacts]
    python -m etl all    （先规划再执行，默认路径开箱即用）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import EtlError
from .planner import Planner, load_plans
from .catalog import Catalog
from .runner import run_plan

DEFAULT_SOURCES = "fixtures/sources.json"
DEFAULT_PLANS = "fixtures/plans.json"
DEFAULT_OUT = "artifacts"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _build_plans(sources_path: str, plans_path: str) -> tuple[Catalog, list[dict]]:
    catalog = Catalog(sources_path)
    return catalog, [Planner(catalog).plan(decl) for decl in load_plans(plans_path)]


def cmd_plan(args: argparse.Namespace) -> int:
    catalog, plans = _build_plans(args.sources, args.plans)
    out_dir = Path(args.out)
    summary: list[dict] = []
    for plan in plans:
        _write_json(out_dir / "plans" / f"{plan['plan_id']}.plan.json", plan)
        summary.append(
            {
                "plan_id": plan["plan_id"],
                "sort_count": plan["sort_count"],
                "injected_sorts": plan["injected_sorts"],
            }
        )
    _write_json(out_dir / "plan_summary.json", {"plans": summary})
    for plan in plans:
        print(f"[plan] {plan['plan_id']}: injected_sorts={plan['sort_count']}")
        for sort_node in plan["injected_sorts"]:
            keys = ", ".join(
                f"{k['field']} {k['direction']} NULLS {k['nulls'].upper()}"
                for k in sort_node["keys"]
            )
            print(f"        + sort {sort_node['id']} on ({keys}) for {sort_node['injected_for']}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    catalog, plans = _build_plans(args.sources, args.plans)
    out_dir = Path(args.out)
    summary: list[dict] = []
    for plan in plans:
        output_rows, report = run_plan(catalog, plan["executable_plan"])
        _write_json(out_dir / "runs" / f"{plan['plan_id']}.run.json", report)
        summary.append(
            {
                "plan_id": plan["plan_id"],
                "sort_call_count": report["sort_call_count"],
                "output_rows": report["total_output_rows"],
            }
        )
        print(
            f"[run]  {plan['plan_id']}: sort_calls={report['sort_call_count']}, "
            f"output_rows={report['total_output_rows']}"
        )
    _write_json(out_dir / "run_summary.json", {"plans": summary})
    return 0


def cmd_all(args: argparse.Namespace) -> int:
    rc = cmd_plan(args)
    if rc:
        return rc
    return cmd_run(args)


def main(argv: list[str] | None = None) -> int:
    # 全局选项统一放在子命令之前：python -m etl [--sources ...] all
    parser = argparse.ArgumentParser(description="物理排序性质驱动的微型 ETL")
    parser.add_argument("--sources", default=DEFAULT_SOURCES)
    parser.add_argument("--plans", default=DEFAULT_PLANS)
    parser.add_argument("--out", default=DEFAULT_OUT)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan").set_defaults(func=cmd_plan)
    sub.add_parser("run").set_defaults(func=cmd_run)
    sub.add_parser("all").set_defaults(func=cmd_all)
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EtlError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
