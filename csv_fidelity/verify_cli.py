"""Independent verifier CLI: ``python -m csv_fidelity.verify_cli``.

Reads the original file, the corrected file, the patch and the map and runs
:func:`csv_fidelity.verify.verify`.  Exits 0 on success, 2 on failure.
"""

from __future__ import annotations

import argparse
import json
import sys

from .errors import PatchError
from .verify import verify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="csv_fidelity.verify_cli",
        description="Independently verify a corrected CSV against its source and map.",
    )
    parser.add_argument("--source", required=True)
    parser.add_argument("--corrected", required=True)
    parser.add_argument("--patch", required=True)
    parser.add_argument("--map", required=True)
    args = parser.parse_args(argv)

    with open(args.source, "rb") as handle:
        source = handle.read()
    with open(args.corrected, "rb") as handle:
        corrected = handle.read()
    with open(args.patch, "r", encoding="utf-8-sig") as handle:
        patch = json.load(handle)
    with open(args.map, "r", encoding="utf-8") as handle:
        result_map = json.load(handle)

    try:
        summary = verify(source, corrected, patch, result_map)
    except PatchError as exc:
        print(f"FAIL: {exc.message}", file=sys.stderr)
        return 2
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
