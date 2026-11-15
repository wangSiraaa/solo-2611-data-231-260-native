"""Command-line entry point for the document-change ingester.

Usage::

    python -m docchange fixtures/initial.json fixtures/patches.jsonl -o output

Reads ``initial.json`` (``{"docs": {doc_id: value}}``), applies every line of
``patches.jsonl`` in order, and writes:

* ``output/documents.json`` - ``{"docs": {...}}`` final document set;
* ``output/batches.jsonl`` - one result record per batch.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .engine import apply_batch
from .errors import InvalidBatch
from .jsonio import check_finite, loads


def load_initial(path: str) -> Dict[str, Any]:
    """Load the initial document store from ``initial.json``."""
    with open(path, "r", encoding="utf-8") as handle:
        data = loads(handle.read())
    if not isinstance(data, dict) or "docs" not in data:
        raise ValueError(f"{path}: expected an object with a 'docs' member")
    docs = data["docs"]
    if not isinstance(docs, dict):
        raise ValueError(f"{path}: 'docs' must be an object")
    for doc_id, document in docs.items():
        if not isinstance(doc_id, str):
            raise ValueError(f"{path}: doc_id keys must be strings")
        check_finite(document, where=f"document {doc_id!r}")
    return docs


def load_batches(path: str) -> List[Tuple[int, Any, Optional[str]]]:
    """Parse every non-blank line of a JSONL file.

    Returns ``(line_number, batch_or_None, error_message)`` triples.  A malformed
    line is represented as a rejected batch rather than aborting the whole run.
    """
    batches: List[Tuple[int, Any, Optional[str]]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                batch = loads(stripped)
            except ValueError as exc:
                batches.append((line_number, None, f"malformed JSON: {exc}"))
                continue
            batches.append((line_number, batch, None))
    return batches


def run(
    initial_path: str, patches_path: str, out_dir: str
) -> Tuple[Dict[str, Any], List[dict]]:
    """Run the ingester and write the two output files."""
    documents = load_initial(initial_path)
    batches = load_batches(patches_path)

    results: List[dict] = []
    for line_number, batch, parse_error in batches:
        if parse_error is not None:
            results.append(
                {
                    "batch_id": None,
                    "doc_id": None,
                    "status": "rejected",
                    "op_index": None,
                    "op": None,
                    "path": None,
                    "from": None,
                    "reason": InvalidBatch.reason,
                    "message": parse_error,
                    "summary_before": None,
                    "summary_after": None,
                    "line": line_number,
                }
            )
            continue
        result = apply_batch(documents, batch)
        result["line"] = line_number
        results.append(result)

    os.makedirs(out_dir, exist_ok=True)
    documents_path = os.path.join(out_dir, "documents.json")
    batches_path = os.path.join(out_dir, "batches.jsonl")

    with open(documents_path, "w", encoding="utf-8") as handle:
        json.dump(
            {"docs": documents},
            handle,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
        )
        handle.write("\n")

    with open(batches_path, "w", encoding="utf-8") as handle:
        for result in results:
            handle.write(
                json.dumps(
                    result,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                )
            )
            handle.write("\n")

    return {"docs": documents}, results


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="docchange",
        description="Apply atomic RFC 6902 patch batches to a JSON document store.",
    )
    parser.add_argument("initial", help="path to initial.json")
    parser.add_argument("patches", help="path to patches.jsonl")
    parser.add_argument(
        "-o",
        "--output",
        default="output",
        help="output directory (default: output)",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        docs, results = run(args.initial, args.patches, args.output)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    accepted = sum(1 for r in results if r["status"] == "accepted")
    rejected = len(results) - accepted
    print(
        f"processed {len(results)} batch(es): {accepted} accepted, "
        f"{rejected} rejected; {len(docs['docs'])} document(s) written",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
