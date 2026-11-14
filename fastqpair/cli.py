"""命令行入口。

用法::

    python -m fastqpair --manifest manifest.json --config config.json --out out/

manifest.json 为来源清单（JSON 数组，每项含 path、mate，可选 compression）；
config.json 为规则配置，缺省时使用内置默认规则。
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .engine import run_pairing
from .model import Config, SourceSpec


def load_manifest(path: str) -> list[SourceSpec]:
    with open(path, "rb") as fh:
        data = json.load(fh)
    if not isinstance(data, list) or not data:
        raise ValueError("manifest 必须是非空 JSON 数组")
    base_dir = os.path.dirname(os.path.abspath(path))
    specs: list[SourceSpec] = []
    for item in data:
        if not isinstance(item, dict) or "path" not in item or "mate" not in item:
            raise ValueError("manifest 每项必须包含 path 与 mate")
        mate = int(item["mate"])
        if mate not in (1, 2):
            raise ValueError(f"manifest mate 只能是 1 或 2: {item}")
        raw_path = str(item["path"])
        full = raw_path if os.path.isabs(raw_path) else os.path.join(base_dir, raw_path)
        specs.append(
            SourceSpec(
                path=full,
                default_mate=mate,
                compression=str(item.get("compression", "auto")),
            )
        )
    return specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="乱序 FASTQ 双端原件配对（仅数据接入，不做比对/反向互补/共识）"
    )
    parser.add_argument("--manifest", required=True, help="R1/R2 来源清单 JSON")
    parser.add_argument("--config", default=None, help="规则配置 JSON（可选）")
    parser.add_argument("--out", required=True, help="产物输出目录")
    parser.add_argument(
        "--sqlite", default=None, help="暂存 SQLite 路径（默认输出目录下 .staging.sqlite）"
    )
    args = parser.parse_args(argv)

    try:
        specs = load_manifest(args.manifest)
        if args.config:
            with open(args.config, "rb") as fh:
                config = Config.from_dict(json.load(fh))
        else:
            config = Config()
        result = run_pairing(specs, config, args.out, args.sqlite)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"fastqpair: 配置或输入错误: {exc}", file=sys.stderr)
        return 1

    print(
        f"complete={result.complete} paired={result.paired_count} "
        f"orphan={len(result.orphans)} ambiguous={len(result.ambiguous)} "
        f"mate_rejected={len(result.mate_rejected)} "
        f"quarantined={len(result.quarantine)} spilled={result.spill_count}"
    )
    if not result.complete:
        for st in result.sources:
            if st.fatal is not None:
                print(
                    f"  未读尾部: {st.path} @stream_offset={st.fatal_tail_offset} "
                    f"length={st.fatal_tail_length} reason={st.fatal.reason}",
                    file=sys.stderr,
                )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
