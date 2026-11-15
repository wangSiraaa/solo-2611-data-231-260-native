"""命令行入口。

用法：
    python -m pgmirror.cli build --input fixtures/changes.bin \
        --contract contract.json --out output
    python -m pgmirror.cli inspect --input fixtures/changes.bin \
        --contract contract.json --through 2 [--sql "SELECT ..."]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .driver import build_outputs, inspect_at


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pgmirror",
        description="把离线 pgoutput v1 消息流转成可查询的 SQLite 关系镜像",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="从空镜像构建 mirror.sqlite 与两份 JSONL")
    build.add_argument("--input", required=True, type=Path, help="带 4 字节大端长度分帧的 pgoutput 文件")
    build.add_argument("--contract", required=True, type=Path, help="contract.json")
    build.add_argument("--out", required=True, type=Path, help="输出目录")

    inspect_p = sub.add_parser(
        "inspect", help="在任一消息边界检查已提交镜像（不落盘）"
    )
    inspect_p.add_argument("--input", required=True, type=Path)
    inspect_p.add_argument("--contract", required=True, type=Path)
    inspect_p.add_argument(
        "--through",
        required=True,
        type=int,
        help="含端点的消息序号（0 基），处理完该消息即停止",
    )
    inspect_p.add_argument("--sql", default=None, help="可选：对临时镜像执行的只读查询")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "build":
        result = build_outputs(args.input, args.contract, args.out)
        summary = {
            "output_dir": str(args.out),
            "messages_consumed": result.messages_consumed,
            "transactions": len(result.transactions),
            "records": len(result.records),
            "fatal": result.fatal,
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if result.ok else 2
    if args.command == "inspect":
        report = inspect_at(args.input, args.contract, args.through, args.sql)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["fatal"] is None else 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
