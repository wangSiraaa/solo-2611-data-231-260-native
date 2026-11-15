"""命令行入口：python -m ttml2cues <input.ttml> [-o OUT_DIR]"""
from __future__ import annotations

import argparse
import sys

from .converter import convert_file
from .timing import TICK_RATE, TimingError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把有限子集 TTML 字幕转成 paragraphs/cues/source_nodes 三个 JSONL"
    )
    parser.add_argument("input", help="输入 .ttml 文件")
    parser.add_argument(
        "-o",
        "--out-dir",
        default="output",
        help="输出目录（默认 output，自动创建）",
    )
    parser.add_argument("--tick-rate", type=int, default=TICK_RATE)
    args = parser.parse_args(argv)

    try:
        paths = convert_file(args.input, args.out_dir, tick_rate=args.tick_rate)
    except TimingError as exc:
        print(f"转换被拒绝: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"输入文件不存在: {exc}", file=sys.stderr)
        return 2

    for kind, path in paths.items():
        print(f"{kind}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
