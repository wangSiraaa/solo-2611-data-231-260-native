"""查询命令行入口：从已物化的历史库做点查询 / 知识切片查询。"""

from __future__ import annotations

import argparse
import json

from .store import query, slice_at


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="查询双时态历史（只读历史表，不重放修订事件）"
    )
    parser.add_argument("entity", help="实体标识")
    parser.add_argument("valid_at", type=int, help="有效时间点（整数）")
    parser.add_argument("known_at", type=int, help="知识时刻 K（整数）")
    parser.add_argument(
        "--db", default="history/bitemporal.db", help="SQLite 历史库路径"
    )
    parser.add_argument(
        "--slice",
        action="store_true",
        help="同时输出该知识切片下 entity 的全部可见段",
    )
    args = parser.parse_args(argv)

    result = query(args.entity, args.valid_at, args.known_at, args.db)
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if args.slice:
        segments = slice_at(args.entity, args.known_at, args.db)
        print(json.dumps({"slice": segments}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
