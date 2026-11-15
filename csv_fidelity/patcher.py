"""Batch validation, field-local replacement and provenance-map construction."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .errors import PatchError, PatchErrorCode
from .scanner import Cell, Document, scan_document

_PATCH_KEYS = {"row", "column", "expected", "value"}
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


@dataclass(frozen=True)
class Change:
    row: int
    column: int
    expected: str
    value: str


@dataclass(frozen=True)
class Replacement:
    row: int
    column: int
    start: int
    end: int
    quoted: bool
    old: str
    new: str
    new_bytes: bytes


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_patch(patch, source_sha256: str) -> list[Change]:
    """Validate the patch envelope and its JSON types.

    Only shape/identity checks happen here; coordinate and old-value checks
    need the scanned document and are done by :func:`plan_replacements`.
    """

    if not isinstance(patch, dict):
        raise PatchError(PatchErrorCode.INVALID_PATCH, "patch must be a JSON object")

    claimed = patch.get("source_sha256")
    if not isinstance(claimed, str) or not _SHA256_RE.fullmatch(claimed):
        raise PatchError(
            PatchErrorCode.INVALID_PATCH, "source_sha256 must be a 64-char hex string"
        )
    if claimed.lower() != source_sha256.lower():
        raise PatchError(
            PatchErrorCode.SHA256_MISMATCH,
            "source file SHA256 does not match the patch",
            expected=claimed.lower(),
            actual=source_sha256.lower(),
        )

    changes = patch.get("changes")
    if not isinstance(changes, list):
        raise PatchError(PatchErrorCode.INVALID_PATCH, "changes must be a list")

    parsed: list[Change] = []
    seen: dict[tuple[int, int], int] = {}
    for index, raw in enumerate(changes):
        if not isinstance(raw, dict):
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                f"changes[{index}] must be an object",
                index=index,
            )
        unknown = set(raw) - _PATCH_KEYS
        if unknown:
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                f"changes[{index}] has unknown key(s): {sorted(unknown)}",
                index=index,
                keys=sorted(unknown),
            )
        missing = _PATCH_KEYS - set(raw)
        if missing:
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                f"changes[{index}] is missing: {sorted(missing)}",
                index=index,
                missing=sorted(missing),
            )
        row, column = raw["row"], raw["column"]
        expected, value = raw["expected"], raw["value"]
        if not _is_int(row) or not _is_int(column):
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                f"changes[{index}] row/column must be integers",
                index=index,
            )
        if not isinstance(expected, str) or not isinstance(value, str):
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                f"changes[{index}] expected/value must be strings",
                index=index,
            )
        if row < 0 or column < 0:
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                f"changes[{index}] has negative coordinate",
                index=index,
                row=row,
                column=column,
            )
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                f"changes[{index}] value is not encodable as UTF-8",
                index=index,
            ) from None

        key = (row, column)
        if key in seen:
            raise PatchError(
                PatchErrorCode.DUPLICATE_CELL,
                f"cell ({row}, {column}) appears more than once",
                row=row,
                column=column,
                first_index=seen[key],
                duplicate_index=index,
            )
        seen[key] = index
        parsed.append(Change(row=row, column=column, expected=expected, value=value))

    return parsed


def render_field(
    new_value: str, originally_quoted: bool, *, single_column_record: bool = False
) -> bytes:
    """Render a replacement lexeme according to the field's original style.

    * originally quoted -> stay quoted and double every inner quote;
    * originally unquoted -> stay unquoted unless the new value contains a
      comma, CR, LF or a double quote, in which case it is upgraded to a
      quoted field (with inner quotes doubled).

    One representability exception: in a *single-column* record an unquoted
    empty value is a zero-length lexeme, which is byte-identical to a blank
    line (a zero-field record in this dialect).  Such a value is emitted as
    ``""`` so the record keeps exactly one empty field.
    """

    upgrade = any(ch in new_value for ch in ',\r\n"')
    encoded = new_value.encode("utf-8")
    if encoded == b"" and single_column_record:
        return b'""'
    if originally_quoted or upgrade:
        return b'"' + encoded.replace(b'"', b'""') + b'"'
    return encoded


def plan_replacements(doc: Document, changes: list[Change]) -> list[Replacement]:
    """Resolve every change against the document, validating the whole batch."""

    replacements: list[Replacement] = []
    for change in changes:
        if change.row >= len(doc.records):
            raise PatchError(
                PatchErrorCode.ROW_OUT_OF_RANGE,
                f"row {change.row} does not exist (data rows: {len(doc.records)})",
                row=change.row,
            )
        if change.column >= doc.width:
            raise PatchError(
                PatchErrorCode.UNKNOWN_COLUMN,
                f"column {change.column} does not exist (header width: {doc.width})",
                column=change.column,
            )
        cell: Cell = doc.records[change.row].cells[change.column]
        if cell.value != change.expected:
            raise PatchError(
                PatchErrorCode.WRONG_OLD_VALUE,
                f"cell ({change.row}, {change.column}) old value mismatch",
                row=change.row,
                column=change.column,
                expected=change.expected,
                actual=cell.value,
            )
        replacements.append(
            Replacement(
                row=change.row,
                column=change.column,
                start=cell.start,
                end=cell.end,
                quoted=cell.quoted,
                old=cell.value,
                new=change.value,
                # An unchanged value must preserve the original lexeme byte for
                # byte rather than be normalised through a renderer.
                new_bytes=(
                    doc.data[cell.start : cell.end]
                    if change.value == cell.value
                    else render_field(
                        change.value,
                        cell.quoted,
                        single_column_record=doc.width == 1,
                    )
                ),
            )
        )

    # Cells are addressed independently, so replacements can only overlap if
    # a scanner/assigner bug produced inconsistent spans; fail loudly.
    # ``(start, end)`` is physical file order; row/column only break ties.
    replacements.sort(key=lambda r: (r.start, r.end, r.row, r.column))
    # Distinct cells can never overlap; guard anyway so a scanner bug cannot
    # corrupt output ordering.
    for previous, current in zip(replacements, replacements[1:]):
        if current.start < previous.end:
            raise PatchError(
                PatchErrorCode.INVALID_PATCH,
                "replacement ranges overlap",
                previous=(previous.row, previous.column),
                current=(current.row, current.column),
            )
    return replacements


def build_corrected(source: bytes, replacements: list[Replacement]) -> tuple[bytes, list[dict]]:
    """Splice replacements into the original bytes; return bytes and segments."""

    segments: list[dict] = []
    target_pos = 0
    cursor = 0
    total = len(source)

    def add_copy(source_start: int, source_end: int) -> None:
        nonlocal target_pos
        length = source_end - source_start
        if length == 0:
            return
        segments.append(
            {
                "kind": "copy",
                "source_start": source_start,
                "source_end": source_end,
                "target_start": target_pos,
                "target_end": target_pos + length,
            }
        )
        target_pos += length

    for replacement in replacements:
        if replacement.start > cursor:
            add_copy(cursor, replacement.start)
        length = len(replacement.new_bytes)
        segments.append(
            {
                "kind": "replace",
                "source_start": replacement.start,
                "source_end": replacement.end,
                "target_start": target_pos,
                "target_end": target_pos + length,
                "cell": {"row": replacement.row, "column": replacement.column},
                "old": replacement.old,
                "new": replacement.new,
            }
        )
        target_pos += length
        cursor = replacement.end
    add_copy(cursor, total)

    if any(s["kind"] == "copy" and s["target_start"] == s["target_end"] for s in segments):
        raise PatchError(
            PatchErrorCode.INTERNAL_ERROR, "copy segment with empty target range"
        )
    expected_target = 0
    for s in segments:
        if s["target_start"] != expected_target:
            raise PatchError(
                PatchErrorCode.INTERNAL_ERROR, "provenance map is not contiguous"
            )
        expected_target = s["target_end"]
    if expected_target != target_pos:
        raise PatchError(
            PatchErrorCode.INTERNAL_ERROR, "provenance map target coverage mismatch"
        )
    if segments and segments[-1]["source_end"] != total:
        raise PatchError(
            PatchErrorCode.INTERNAL_ERROR, "provenance map source coverage mismatch"
        )

    out: list[bytes] = []
    source_cursor = 0
    for replacement in replacements:
        out.append(source[source_cursor : replacement.start])
        out.append(replacement.new_bytes)
        source_cursor = replacement.end
    out.append(source[source_cursor:])
    output = b"".join(out)
    if len(output) != target_pos:
        raise PatchError(
            PatchErrorCode.INTERNAL_ERROR,
            "built output length disagrees with the provenance map",
        )
    return output, segments


def apply_patch(source: bytes, patch) -> dict:
    """Validate and apply a patch in memory.

    Returns ``{"output": bytes, "map": dict}``.  Raises :class:`PatchError`
    without producing output if any part of the batch is invalid.
    """

    source = bytes(source)
    source_sha = sha256_hex(source)
    changes = parse_patch(patch, source_sha)
    doc = scan_document(source)
    replacements = plan_replacements(doc, changes)
    output, segments = build_corrected(source, replacements)
    result_map = {
        "source_sha256": source_sha,
        "target_sha256": sha256_hex(output),
        "source_size": len(source),
        "target_size": len(output),
        "segments": segments,
    }
    return {"output": output, "map": result_map, "document": doc}
