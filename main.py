#!/usr/bin/env python3
"""ETL 入口：读取 fixtures，预检编译，按批大小执行，写出编译计划/输出/trace。

用法: python3 main.py [--fixtures fixtures] [--out out] [--batch-size 3]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from etl.model import CompileError
from etl.runner import run_job


def main() -> int:
    ap = argparse.ArgumentParser(description="受限列式表达式 ETL")
    ap.add_argument("--fixtures", default="fixtures", help="输入目录")
    ap.add_argument("--out", default="out", help="输出目录")
    ap.add_argument("--batch-size", type=int, default=3, help="批大小（>=1）")
    args = ap.parse_args()

    try:
        result = run_job(Path(args.fixtures), args.batch_size)
    except CompileError as e:
        print(f"预检拒绝，未执行任何行: {e}", file=sys.stderr)
        return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    _write(out_dir / "compile_plan.json", result.compile_plan_dict())
    _write(out_dir / "output.json", result.output_dict())
    _write(out_dir / "trace.json", result.trace_dict())
    print(f"完成: {result.n_rows} 行 x {len(result.plans)} 表达式, "
          f"batch_size={result.batch_size} -> {out_dir}/")
    return 0


def _write(path: Path, obj) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


if __name__ == "__main__":
    sys.exit(main())
