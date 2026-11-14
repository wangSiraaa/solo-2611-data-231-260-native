"""Command-line entry point: consume stream.jsonl row by row and emit results.

The formal processing path never reads the whole stream into a list and sorts
it; each JSONL line is handed to ``Matcher.push`` (or ``Matcher.end`` for a
control record) immediately upon arrival.  ``events.csv`` is reference-only and
is not consulted here.

``--emit-each`` prints the output of every push/END as it happens, which shows
matches are finalised during streaming rather than in one shot at partition end.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator

from .events import Event, Pattern, RejectedInput
from .matcher import Matcher

DEFAULT_PATTERNS = Path("fixtures/patterns.json")
DEFAULT_STREAM = Path("fixtures/stream.jsonl")


def load_patterns(path: str | Path) -> list[Pattern]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return [Pattern.from_dict(obj) for obj in data]


def _iter_stream(path: str) -> Iterator[tuple[int, dict]]:
    """Yield parsed JSON objects lazily, one physical line at a time."""
    if path == "-":
        stream = sys.stdin
        close = False
    else:
        stream = open(path, "r", encoding="utf-8")
        close = True
    try:
        for lineno, line in enumerate(stream, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield lineno, json.loads(line)
            except json.JSONDecodeError as exc:
                raise RejectedInput(f"line {lineno}: invalid JSON: {exc}") from exc
    finally:
        if close:
            stream.close()


def run(
    patterns: list[Pattern],
    stream_path: str,
    emit_each: bool = False,
    out=None,
):
    """Stream the input through the matcher and build a JSON-serialisable report.

    With ``emit_each=True`` the per-push output is written to ``out`` (default
    stdout) as each record is consumed.
    """
    matcher = Matcher(patterns)
    rejected: list[dict] = []
    accepted = 0

    for lineno, obj in _iter_stream(stream_path):
        record_type = obj.get("type", "row")
        try:
            if record_type == "end":
                emitted = matcher.end(obj["partition"])
                trigger = {"kind": "end", "partition": obj["partition"]}
            elif record_type == "row":
                emitted = matcher.push(Event.from_dict(obj))
                trigger = {
                    "kind": "data",
                    "partition": obj["partition"],
                    "data_index": matcher.partition_info()[obj["partition"]]["data_rows"],
                }
            else:
                raise RejectedInput(f"line {lineno}: unknown record type {record_type!r}")
            accepted += 1
        except (RejectedInput, KeyError, TypeError, ValueError) as exc:
            rejected.append(
                {"line": lineno, "type": record_type, "reason": str(exc) or exc.__class__.__name__}
            )
            if emit_each and out is not None:
                out.write(
                    json.dumps(
                        {
                            "line": lineno,
                            "type": record_type,
                            "rejected": str(exc) or exc.__class__.__name__,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            continue

        if emit_each and out is not None:
            pending = {}
            if trigger["partition"] in matcher.partition_info():
                for p in patterns:
                    pending[p.id] = {
                        "pending_start_seq": matcher.pending_start(p.id, trigger["partition"]),
                        "pending_len": matcher.pending_len(p.id, trigger["partition"]),
                        "buffer_peak": matcher.peak(p.id, trigger["partition"]),
                    }
            out.write(
                json.dumps(
                    {
                        "line": lineno,
                        "type": record_type,
                        "trigger": trigger,
                        "emitted": emitted,
                        "pending": pending,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

    return {
        "patterns": [
            {
                "id": p.id,
                "variables": p.variables,
                "min": p.mins,
                "max": p.maxs,
                "skip": p.skip,
            }
            for p in patterns
        ],
        "accepted_records": accepted,
        "rejected": rejected,
        "partitions": matcher.partition_info(),
        "results": matcher.results,
        "push_log": matcher.log,
        "buffer_peaks": matcher.peaks(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Incrementally extract contiguous pattern segments from an interleaved JSONL stream."
    )
    parser.add_argument(
        "--patterns", default=str(DEFAULT_PATTERNS),
        help="pattern definitions JSON (default: %(default)s)",
    )
    parser.add_argument(
        "--stream", default=str(DEFAULT_STREAM),
        help="JSONL stream; use '-' for stdin (default: %(default)s)",
    )
    parser.add_argument("--output", default="-", help="write the JSON report here ('-' = stdout)")
    parser.add_argument(
        "--results-only", action="store_true", help="print just the list of match results"
    )
    parser.add_argument(
        "--emit-each",
        action="store_true",
        help="print every push/END output as it happens (append the report to stderr)",
    )
    args = parser.parse_args(argv)

    try:
        patterns = load_patterns(args.patterns)
    except (OSError, ValueError, KeyError) as exc:
        print(f"error: cannot load patterns: {exc}", file=sys.stderr)
        return 2

    if args.emit_each:
        # Live per-push output goes to stdout; the aggregated report to stderr.
        live = sys.stdout
        report = run(patterns, args.stream, emit_each=True, out=live)
        print(json.dumps({k: report[k] for k in ("accepted_records", "rejected")},
                         ensure_ascii=False), file=sys.stderr)
        return 0

    try:
        report = run(patterns, args.stream)
    except (OSError, RejectedInput) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    payload = report["results"] if args.results_only else report
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output == "-":
        print(text)
    else:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
