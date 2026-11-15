"""Command-line entry point for the byte-faithful CSV patcher.

Usage::

    python -m csv_fidelity --source fixtures/source.csv \
        --patch fixtures/patch.json \
        --corrected out/corrected.csv --map out/map.json

The source is opened read-only and is never used as an output path.  All
validation happens before anything is written; outputs are staged in temporary
files and atomically renamed, so a rejected batch publishes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

from .errors import PatchError, PatchErrorCode
from .patcher import apply_patch
from .verify import verify


def _guard_outputs(source_path: str, outputs: list[str]) -> None:
    source_real = os.path.realpath(source_path)
    source_stat = os.stat(source_path)
    for output in outputs:
        real = os.path.realpath(output)
        if real == source_real:
            raise PatchError(
                PatchErrorCode.OUTPUT_IS_SOURCE,
                f"output path {output!r} aliases the source file",
                path=output,
            )
        if os.path.exists(output):
            st = os.stat(output)
            if (st.st_dev, st.st_ino) == (source_stat.st_dev, source_stat.st_ino):
                raise PatchError(
                    PatchErrorCode.OUTPUT_IS_SOURCE,
                    f"output path {output!r} is a hard link to the source",
                    path=output,
                )


def _stage(path: str, data: bytes) -> str:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
    except BaseException:
        os.unlink(tmp)
        raise
    return tmp


def run(args: argparse.Namespace) -> int:
    with open(args.source, "rb") as handle:
        source = handle.read()

    with open(args.patch, "rb") as handle:
        raw_patch = handle.read()
    try:
        patch = json.loads(raw_patch.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise PatchError(
            PatchErrorCode.INVALID_PATCH, "patch file is not valid UTF-8"
        ) from None
    except json.JSONDecodeError as exc:
        raise PatchError(
            PatchErrorCode.INVALID_PATCH,
            f"patch is not valid JSON: {exc.msg}",
            line=exc.lineno,
            column=exc.colno,
        ) from None

    _guard_outputs(args.source, [args.corrected, args.map])

    result = apply_patch(source, patch)
    # Self-check before publishing: the verifier is independent of the
    # scanner/splicer and must agree that only the requested cells changed.
    verify(source, result["output"], patch, result["map"])
    map_bytes = (
        json.dumps(result["map"], ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")

    staged_corrected = _stage(args.corrected, result["output"])
    try:
        staged_map = _stage(args.map, map_bytes)
    except BaseException:
        os.unlink(staged_corrected)
        raise

    os.replace(staged_corrected, args.corrected)
    os.replace(staged_map, args.map)

    print(
        f"ok: {len(result['map']['segments'])} segments, "
        f"{result['map']['source_size']} -> {result['map']['target_size']} bytes",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="csv_fidelity",
        description="Byte-faithful CSV cell patcher (source file is read-only).",
    )
    parser.add_argument("--source", required=True, help="UTF-8 CSV to read (never written)")
    parser.add_argument("--patch", required=True, help="patch JSON bound to the source SHA256")
    parser.add_argument("--corrected", required=True, help="path for the corrected CSV")
    parser.add_argument("--map", required=True, help="path for the provenance map JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except PatchError as exc:
        print(f"error: {exc.code.value}: {exc.message}", file=sys.stderr)
        print(json.dumps(exc.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: I/O failure: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
