"""Command line entry point for building an HDF5 delivery.

Usage::

    python -m hdf5_delivery build fixtures/root.h5 delivery.h5 \
        --manifest delivery.manifest.json

The process exits non-zero on any failure and never leaves a partial delivery
behind.
"""

from __future__ import annotations

import argparse
import json
import sys

from .builder import build_delivery
from .errors import DeliveryError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hdf5_delivery",
        description=(
            "Copy an HDF5 file and all reachable external dependencies into a "
            "single self-contained delivery file plus a manifest."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build delivery.h5 and manifest.json")
    build.add_argument("input", help="root HDF5 file to deliver")
    build.add_argument("output", help="output HDF5 file (delivery.h5)")
    build.add_argument(
        "--manifest",
        help="manifest path (default: manifest.json next to the output)",
    )
    build.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing output/manifest files",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command != "build":  # pragma: no cover - argparse enforces
        raise SystemExit(2)
    try:
        manifest = build_delivery(
            args.input,
            args.output,
            args.manifest,
            overwrite=args.overwrite,
        )
    except DeliveryError as exc:
        print(f"delivery failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        print("delivery interrupted", file=sys.stderr)
        return 130
    summary = {
        "output": manifest["output"]["path"],
        "manifest": manifest["output"]["manifest_path"],
        "counts": manifest["counts"],
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
