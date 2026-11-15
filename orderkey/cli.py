"""命令行入口：用 fixtures 或自定义文件构建索引并执行前缀扫描。

示例：
  python -m orderkey.cli build --schema fixtures/schema.json \\
      --rows fixtures/rows.jsonl --db /tmp/demo.db
  python -m orderkey.cli scan --db /tmp/demo.db \\
      --equals '["A", 5]'
  python -m orderkey.cli scan --db /tmp/demo.db --starts-with A
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from .codec import compile_prefix
from .store import Store


def _load_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _build(args: argparse.Namespace) -> None:
    with open(args.schema, encoding="utf-8") as f:
        schema = json.load(f)
    rows = _load_jsonl(args.rows)
    store = Store.build(args.db, schema, rows)
    print(f"已构建 {args.db}：写入 {store.count()} 行，格式版本 1")
    store.close()


def _scan(args: argparse.Namespace) -> None:
    equals: list[Any] = json.loads(args.equals) if args.equals else []
    store = Store.open(args.db)
    lower, upper = compile_prefix(store.schema, equals, args.starts_with)
    result = store.scan(equals, args.starts_with)
    print(f"schema: {json.dumps(store.schema, ensure_ascii=False)}")
    print(f"区间  : {result.describe_bounds()}  (下界包含，上界排除；None=无穷)")
    print(f"SQL   : {result.sql}")
    for plan in result.explain:
        print(f"PLAN  : {plan}")
    print(f"读取键数={result.keys_read}  返回键数={result.keys_returned}")
    for record in result.rows:
        print(f"  {record['row_id']}: {json.dumps(record['key'], ensure_ascii=False)}")
    store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="复合业务键 BLOB 字节序索引")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_build = sub.add_parser("build", help="构建 SQLite BLOB 索引")
    p_build.add_argument("--schema", required=True)
    p_build.add_argument("--rows", required=True, help="JSONL 行文件")
    p_build.add_argument("--db", required=True)
    p_build.set_defaults(func=_build)

    p_scan = sub.add_parser("scan", help="前缀区间扫描")
    p_scan.add_argument("--db", required=True)
    p_scan.add_argument("--equals", help='JSON 数组，如 \'["A", 5]\'')
    p_scan.add_argument("--starts-with", dest="starts_with")
    p_scan.set_defaults(func=_scan)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
