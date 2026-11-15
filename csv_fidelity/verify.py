"""Independent verification of a fidelity patch run.

Checks are deliberately independent of :mod:`csv_fidelity.patcher`:

1. ``csv.reader(newline="")`` reads both files and confirms that exactly the
   specified logical values changed (and nothing else);
2. the map is replayed segment by segment: ``copy`` segments must be
   byte-identical between source and target, ``replace`` segments must be
   lexically valid for the target dialect and decode to the requested value;
3. the segments must tile both files completely, in order, without overlap or
   omission (zero-length source ranges are allowed only for replacements of
   empty fields and must stay ordered);
4. bytes outside replacement target ranges must be byte-identical, which also
   catches terminator/BOM/final-newline drift and any whole-file re-serialise.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json

from .errors import PatchError, PatchErrorCode
from .scanner import scan_document


def _read_logical(data: bytes) -> list[list[str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(
            PatchErrorCode.INVALID_UTF8,
            f"CSV is not valid UTF-8 at byte {exc.start}",
            offset=exc.start,
        ) from None
    if text.startswith("﻿"):
        text = text[1:]
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        return [list(row) for row in reader]
    except csv.Error as exc:
        raise PatchError(
            PatchErrorCode.INVALID_PATCH,
            f"csv.reader rejected the data: {exc}",
        ) from None


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(source: bytes, target: bytes, patch: dict, result_map: dict) -> dict:
    """Raise :class:`PatchError` on the first invariant violation.

    Returns a small summary dict for the caller/tests.
    """

    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            failures.append(message)

    def finish(summary: dict) -> dict:
        if failures:
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                "verification failed: " + "; ".join(failures),
                failures=failures,
            )
        return summary

    # ---- envelope ---------------------------------------------------------
    check(_sha(source) == patch["source_sha256"], "source SHA256 mismatch")
    check(result_map.get("source_sha256") == _sha(source), "map source_sha256 mismatch")
    check(result_map.get("target_sha256") == _sha(target), "map target_sha256 mismatch")
    check(result_map.get("source_size") == len(source), "map source_size mismatch")
    check(result_map.get("target_size") == len(target), "map target_size mismatch")
    if failures:
        return finish({})

    # ---- expected logical matrix from patch -------------------------------
    original_rows = _read_logical(source)
    corrected_rows = _read_logical(target)
    check(len(original_rows) == len(corrected_rows), "row count changed")

    requested: dict[tuple[int, int], tuple[str, str]] = {}
    expected_rows = [list(row) for row in original_rows]
    for change in patch["changes"]:
        row, column = change["row"], change["column"]
        key = (row, column)
        if (
            row < 0
            or column < 0
            or row + 1 >= len(expected_rows)
            or column >= len(expected_rows[row + 1])
        ):
            failures.append(f"patch cell ({row}, {column}) out of range")
            continue
        if expected_rows[row + 1][column] != change["expected"]:
            failures.append(f"patch expected value wrong at ({row}, {column})")
        if key in requested:
            failures.append(f"patch repeats cell ({row}, {column})")
        requested[key] = (change["expected"], change["value"])
        expected_rows[row + 1][column] = change["value"]

    # Header must be untouched; every non-patched cell identical; every
    # patched cell equal to the requested value.
    if original_rows and corrected_rows:
        check(original_rows[0] == corrected_rows[0], "header values changed")
    for row_index in range(1, min(len(original_rows), len(corrected_rows))):
        if len(original_rows[row_index]) != len(corrected_rows[row_index]):
            continue  # reported by the field-count check below
        for column in range(len(original_rows[row_index])):
            key = (row_index - 1, column)
            if key in requested:
                check(
                    corrected_rows[row_index][column] == expected_rows[row_index][column],
                    f"cell {key} not updated to requested value",
                )
            else:
                check(
                    corrected_rows[row_index][column]
                    == original_rows[row_index][column],
                    f"unpatched cell {key} changed",
                )
    check(
        [len(r) for r in original_rows] == [len(r) for r in corrected_rows],
        "field counts changed",
    )

    # ---- map tiling -------------------------------------------------------
    segments = result_map.get("segments")
    if not isinstance(segments, list):
        failures.append("map segments missing")
        return finish({})

    source_cursor = 0
    target_cursor = 0
    seen_replacements: dict[tuple[int, int], dict] = {}
    for index, segment in enumerate(segments):
        kind = segment.get("kind")
        ss, se = segment["source_start"], segment["source_end"]
        ts, te = segment["target_start"], segment["target_end"]
        if ss != source_cursor:
            failures.append(f"segment {index}: source range is not contiguous")
        if ts != target_cursor:
            failures.append(f"segment {index}: target range is not contiguous")
        if se < ss or te < ts:
            failures.append(f"segment {index}: inverted range")
        if kind == "copy":
            if (se - ss) != (te - ts):
                failures.append(f"segment {index}: copy length mismatch")
            elif source[ss:se] != target[ts:te]:
                failures.append(f"segment {index}: copy bytes differ")
            if se == ss:
                failures.append(f"segment {index}: empty copy segment")
        elif kind == "replace":
            cell = segment.get("cell") or {}
            key = (cell.get("row"), cell.get("column"))
            problem = _check_lexeme(
                target[ts:te],
                source[ss:se],
                segment.get("old"),
                segment.get("new"),
            )
            if problem:
                failures.append(f"segment {index}: {problem}")
            else:
                expected_pair = requested.get(key)
                if expected_pair is None:
                    failures.append(f"segment {index}: replaces an unrequested cell")
                elif expected_pair != (segment.get("old"), segment.get("new")):
                    failures.append(f"segment {index}: old/new differ from patch")
            seen_replacements[key] = segment
        else:
            failures.append(f"segment {index}: unknown kind {kind!r}")
        source_cursor = se
        target_cursor = te

    if source_cursor != len(source):
        failures.append("map does not cover the whole source")
    if target_cursor != len(target):
        failures.append("map does not cover the whole target")
    check(
        set(seen_replacements) == set(requested),
        "map replacement cells differ from patch cells",
    )

    # ---- BOM / header / terminators / final newline -----------------------
    original_doc = scan_document(source)
    target_doc = scan_document(target)
    check(original_doc.bom == target_doc.bom, "BOM presence/content changed")
    check(
        source[original_doc.header_start : original_doc.header_end]
        == target[target_doc.header_start : target_doc.header_end],
        "header lexeme bytes changed",
    )
    check(
        source[slice(*original_doc.header_terminator)]
        == target[slice(*target_doc.header_terminator)],
        "header record terminator changed",
    )
    check(
        len(original_doc.records) == len(target_doc.records),
        "record count changed",
    )
    if len(original_doc.records) == len(target_doc.records):
        for row_index, (a, b) in enumerate(
            zip(original_doc.records, target_doc.records)
        ):
            check(
                source[a.terminator_start : a.terminator_end]
                == target[b.terminator_start : b.terminator_end],
                f"row {row_index} record terminator changed",
            )
    check(
        original_doc.last_record_terminated == target_doc.last_record_terminated,
        "final record terminator presence changed",
    )

    # ---- untouched fields keep their exact lexeme -------------------------
    # Every original cell that is not a replacement target must have been
    # copied byte-for-byte (implied by the tiling, but asserted explicitly).
    replacement_spans = [
        (s["source_start"], s["source_end"])
        for s in segments
        if s.get("kind") == "replace"
    ]
    for record in original_doc.records:
        for cell in record.cells:
            if any(start <= cell.start and cell.end <= end for start, end in replacement_spans):
                continue
            # Locate the corresponding target cell by logical position.
            target_cell = target_doc.records[record.row].cells[cell.column]
            if source[cell.start : cell.end] != target[
                target_cell.start : target_cell.end
            ]:
                failures.append(
                    f"untouched cell ({record.row}, {cell.column}) bytes changed"
                )

    if failures:
        raise PatchError(
            PatchErrorCode.INVALID_PATCH,
            "verification failed: " + "; ".join(failures),
            failures=failures,
        )
    return {
        "ok": True,
        "failures": [],
        "changes": len(requested),
        "segments": len(segments),
    }


def _check_lexeme(
    lexeme: bytes, original: bytes, old: str | None, new: str | None
) -> str | None:
    """Validate a replacement lexeme decodes to ``new`` with legal quoting.

    ``original`` is the source lexeme for the same field, used to enforce the
    style contract: quoted stays quoted (including same-value edits, which
    must keep the exact source bytes); unquoted stays unquoted unless the new
    value needs quoting.
    """

    if old is None or new is None:
        return "replace segment lacks old/new values"
    try:
        text = lexeme.decode("utf-8")
        original_text = original.decode("utf-8")
    except UnicodeDecodeError:
        return "replacement lexeme is not UTF-8"

    original_quoted = original_text.startswith('"')
    target_quoted = text.startswith('"')

    if old == new:
        if lexeme != original:
            return "same-value replacement changed the original bytes"
        return None

    if original_quoted and not target_quoted:
        return "quoted source field was emitted unquoted"

    if target_quoted:
        if len(text) < 2 or not text.endswith('"'):
            return "replacement has unbalanced quotes"
        body = text[1:-1]
        decoded_parts: list[str] = []
        i = 0
        while i < len(body):
            if body[i] == '"':
                if i + 1 < len(body) and body[i + 1] == '"':
                    decoded_parts.append('"')
                    i += 2
                    continue
                return "replacement has a bare internal quote"
            decoded_parts.append(body[i])
            i += 1
        decoded = "".join(decoded_parts)
        if decoded != new:
            return f"replacement decodes to {decoded!r}, expected {new!r}"
        return None

    # Unquoted lexeme: only legal if the source was unquoted and no quote
    # upgrade conditions apply.
    if original_quoted:
        return "quoted source field was emitted unquoted"
    if any(ch in text for ch in ',\r\n"'):
        return "unquoted replacement contains a delimiter, newline or quote"
    if text != new:
        return f"replacement decodes to {text!r}, expected {new!r}"
    return None
