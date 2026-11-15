"""Command-line entry point.

Usage::

    python -m evjoin.cli --config config.json --events fixtures/events.jsonl --out out
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

from .joiner import JoinError, Joiner, sort_facts


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")


def run(config_path: str, events_path: str, out_dir: str) -> dict:
    """Consume events in arrival order and write matches/late/trace files."""
    joiner = Joiner.from_config(config_path)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    total = 0
    with open(events_path, "r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            text = raw.strip()
            if not text:
                continue  # blank lines are not records
            total += 1
            try:
                joiner.push(text, line_no=line_no)
            except JoinError:
                raise
            except Exception as exc:  # normalize unexpected failures
                raise JoinError(f"unexpected error: {exc}", line_no) from exc

    joiner.finish()

    facts = sort_facts(joiner.facts)
    _write_jsonl(out / "matches.jsonl", facts)
    _write_jsonl(out / "late.jsonl", joiner.late)
    _write_jsonl(out / "trace.jsonl", joiner.trace)

    return {
        "events": total,
        "matches": len(joiner.matches),
        "unmatched": len(joiner.facts) - len(joiner.matches),
        "late": len(joiner.late),
        "facts": len(facts),
        "out": str(out),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Streaming dual-input event-time LEFT JOIN")
    parser.add_argument("--config", default="config.json", help="path to config.json")
    parser.add_argument("--events", default="fixtures/events.jsonl", help="JSONL event input")
    parser.add_argument("--out", default="out", help="output directory")
    args = parser.parse_args(argv)

    try:
        summary = run(args.config, args.events, args.out)
    except JoinError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
