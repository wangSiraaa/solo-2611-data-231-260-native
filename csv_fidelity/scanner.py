"""Strict, position-preserving CSV scanner.

The scanner works on **bytes** (so offsets are byte offsets, correct for CJK
text) and understands exactly the dialect used by the input:

* optional UTF-8 BOM at offset 0, kept outside the document model;
* comma separates fields;
* ``"`` quotes a field and is escaped by doubling (``""``);
* a quoted field may contain commas, CR, LF, and CRLF;
* a record ends at CRLF, LF, or a lone CR (mixed terminators allowed, in
  line with ``csv.reader(newline="")``).  CR inside a quoted field is data.
  Terminator bytes are copied verbatim; no newline normalisation occurs.
  Blank lines are ordinary zero-field records, so they fail the header-width
  check rather than being skipped;
* a trailing terminator is optional; no empty-line skipping, no newline
  normalisation.

Invalid constructs raise :class:`PatchError` immediately.  The scanner never
uses ``csv``; it is an independent lexical authority.  ``csv.reader`` is only
used by the verifier and the tests as a cross-check.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import PatchError, PatchErrorCode

BOM = b"\xef\xbb\xbf"
COMMA = 0x2C
QUOTE = 0x22
CR = 0x0D
LF = 0x0A


@dataclass
class Cell:
    """One logical field.

    ``start``/``end`` are byte offsets covering the raw lexeme **including**
    the enclosing quotes (if any) and excluding the delimiter comma, the
    record terminator and the BOM.  ``quoted`` records whether the field was
    originally enclosed in double quotes.  ``value`` is the decoded string.
    """

    row: int
    column: int
    start: int
    end: int
    quoted: bool
    value: str


@dataclass
class Record:
    row: int
    cells: list[Cell]
    # Byte span of the record payload, from the first byte of its first field
    # through the last byte of its last field.  Terminator bytes are excluded.
    start: int
    end: int
    terminator_start: int
    terminator_end: int


@dataclass
class Document:
    data: bytes
    bom: bytes
    bom_len: int
    header: list[str]
    header_cells: list[Cell]
    header_start: int
    header_end: int
    header_terminator: tuple[int, int]
    records: list[Record] = field(default_factory=list)
    last_record_terminated: bool = True

    @property
    def width(self) -> int:
        return len(self.header)

    def cell(self, row: int, column: int) -> Cell:
        return self.records[row].cells[column]


def _decode(raw: bytes, *, offset: int) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(
            PatchErrorCode.INVALID_UTF8,
            f"invalid UTF-8 at byte {offset + exc.start}",
            offset=offset + exc.start,
            reason=exc.reason,
        ) from None


def _decode_parts(parts: list[tuple[int, bytes]]) -> str:
    """Decode field fragments and report the true file offset of bad UTF-8.

    ``parts`` pairs each fragment with its byte offset in the source, so an
    error inside an escaped quoted field points at the offending byte rather
    than at the opening quote or a character index.
    """

    joined = b"".join(chunk for _, chunk in parts)
    try:
        return joined.decode("utf-8")
    except UnicodeDecodeError as exc:
        base = 0
        for source_offset, chunk in parts:
            if exc.start < base + len(chunk):
                absolute = source_offset + (exc.start - base)
                break
            base += len(chunk)
        else:  # pragma: no cover - defensive
            absolute = parts[0][0] if parts else 0
        raise PatchError(
            PatchErrorCode.INVALID_UTF8,
            f"invalid UTF-8 at byte {absolute}",
            offset=absolute,
            reason=exc.reason,
        ) from None


def _fail(code: PatchErrorCode, message: str, **details):
    raise PatchError(code, message, **details)


def _parse_record(data: bytes, start: int, row_number: int) -> tuple[list[Cell], int, int]:
    """Parse one logical record starting at ``start``.

    Returns ``(cells, terminator_start, terminator_end)``.  ``row_number`` is
    ``-1`` for the header and the zero-based data-row index otherwise.
    """

    n = len(data)
    pos = start
    cells: list[Cell] = []

    # A record that begins with a terminator has no fields at all.  Blank
    # lines are not skipped; they are records with zero columns and therefore
    # fail the column-count check (unless the header itself has zero columns).
    if pos < n and data[pos] in (CR, LF):
        term_start = pos
        if data[pos] == CR and pos + 1 < n and data[pos + 1] == LF:
            pos += 2
        else:
            pos += 1
        return cells, term_start, pos

    while True:
        cell_start = pos
        column = len(cells)
        quoted = False
        value_parts: list[tuple[int, bytes]] = []

        if pos < n and data[pos] == QUOTE:
            quoted = True
            pos += 1
            closed = False
            while pos < n:
                b = data[pos]
                if b == QUOTE:
                    if pos + 1 < n and data[pos + 1] == QUOTE:
                        value_parts.append((pos, b'"'))
                        pos += 2
                        continue
                    pos += 1  # consume the closing quote
                    closed = True
                    break
                value_parts.append((pos, data[pos : pos + 1]))
                pos += 1
            if not closed:
                _fail(
                    PatchErrorCode.UNCLOSED_QUOTE,
                    f"unclosed quoted field starting at byte {cell_start}",
                    offset=cell_start,
                )
            if pos < n and data[pos] not in (COMMA, CR, LF):
                _fail(
                    PatchErrorCode.TRAILING_AFTER_QUOTE,
                    f"extra byte after closing quote at byte {pos}",
                    offset=pos,
                )
        else:
            while pos < n and data[pos] not in (COMMA, CR, LF):
                if data[pos] == QUOTE:
                    _fail(
                        PatchErrorCode.BARE_QUOTE,
                        f"bare double quote in unquoted field at byte {pos}",
                        offset=pos,
                    )
                value_parts.append((pos, data[pos : pos + 1]))
                pos += 1

        value = _decode_parts(value_parts)
        cells.append(
            Cell(
                row=row_number,
                column=column,
                start=cell_start,
                end=pos,
                quoted=quoted,
                value=value,
            )
        )

        if pos < n and data[pos] == COMMA:
            pos += 1
            continue
        break

    term_start = pos
    if pos < n and data[pos] == CR:
        if pos + 1 < n and data[pos + 1] == LF:
            pos += 2
        else:
            pos += 1  # lone CR is also a record terminator
    elif pos < n and data[pos] == LF:
        pos += 1
    # ``pos == n`` here means an allowed unterminated final record.

    return cells, term_start, pos


def scan_document(data: bytes, *, allowed_columns: int | None = None) -> Document:
    """Lex ``data`` into a :class:`Document` or raise :class:`PatchError`."""

    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("scan_document expects bytes")
    data = bytes(data)

    pos = 0
    bom = b""
    if data.startswith(BOM):
        bom = BOM
        pos = len(BOM)
    if pos >= len(data):
        _fail(PatchErrorCode.EMPTY_DOCUMENT, "document has no header record")

    raw: list[tuple[list[Cell], int, int]] = []
    row_number = -1  # header first
    while True:
        cells, term_start, term_end = _parse_record(data, pos, row_number)
        raw.append((cells, term_start, term_end))
        pos = term_end
        if pos >= len(data):
            break
        row_number += 1

    header_cells, header_term_start, header_term_end = raw[0]
    if not header_cells:
        _fail(PatchErrorCode.EMPTY_DOCUMENT, "document has no header fields")
    width = len(header_cells)
    header = [c.value for c in header_cells]

    if allowed_columns is not None and width != allowed_columns:
        _fail(
            PatchErrorCode.COLUMN_COUNT_MISMATCH,
            f"header has {width} columns, expected {allowed_columns}",
            expected=allowed_columns,
            actual=width,
        )

    records: list[Record] = []
    for row_index, (cells, term_start, term_end) in enumerate(raw[1:]):
        if len(cells) != width:
            _fail(
                PatchErrorCode.COLUMN_COUNT_MISMATCH,
                f"row {row_index} has {len(cells)} fields, header has {width}",
                row=row_index,
                expected=width,
                actual=len(cells),
            )
        for column, cell in enumerate(cells):
            cell.row = row_index
            cell.column = column
        records.append(
            Record(
                row=row_index,
                cells=cells,
                start=cells[0].start,
                end=cells[-1].end,
                terminator_start=term_start,
                terminator_end=term_end,
            )
        )

    last_terminated = raw[-1][2] > raw[-1][1]

    return Document(
        data=data,
        bom=bom,
        bom_len=len(bom),
        header=header,
        header_cells=header_cells,
        header_start=header_cells[0].start,
        header_end=header_cells[-1].end,
        header_terminator=(header_term_start, header_term_end),
        records=records,
        last_record_terminated=last_terminated,
    )
