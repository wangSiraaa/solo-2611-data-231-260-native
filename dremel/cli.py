"""命令行入口：

python -m dremel encode --schema schema.json --rows rows.jsonl --out output/
python -m dremel decode --schema schema.json --in output/ --out restored.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .decode import decode_directory
from .encode import ColumnEncoder
from .metadata import build_meta, write_meta
from .schema import load_schema


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_jsonl(path: Path) -> list:
    rows = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path} 第 {line_no} 行 JSON 非法：{exc}") from exc
    return rows


def cmd_encode(args: argparse.Namespace) -> None:
    schema = load_schema(_load_json(Path(args.schema)))
    rows = _load_jsonl(Path(args.rows))
    encoder = ColumnEncoder(schema)
    normalized = encoder.encode_all(rows)
    encoder.write(args.out)
    meta = build_meta(encoder.schema, encoder.plans)
    write_meta(meta, len(normalized), args.out)
    print(f"encoded {len(normalized)} records -> {args.out}")
    for path, entries in sorted(encoder.columns.items()):
        print(f"  {'.'.join(path):24s} {len(entries):3d} entries")


def cmd_decode(args: argparse.Namespace) -> None:
    records = decode_directory(args.indir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for row in records:
            f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"decoded {len(records)} records -> {out}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="dremel", description="受限 Dremel 式列化/反解")
    sub = parser.add_subparsers(dest="cmd", required=True)

    enc = sub.add_parser("encode", help="schema + rows.jsonl -> 叶列流目录")
    enc.add_argument("--schema", required=True)
    enc.add_argument("--rows", required=True)
    enc.add_argument("--out", required=True)
    enc.set_defaults(func=cmd_encode)

    dec = sub.add_parser("decode", help="叶列流目录 -> restored.jsonl")
    dec.add_argument("--schema", required=False, help="可选：外部 schema 交叉校验（默认读列目录内 _meta.json）")
    dec.add_argument("--in", dest="indir", required=True)
    dec.add_argument("--out", required=True)
    dec.set_defaults(func=cmd_decode)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
