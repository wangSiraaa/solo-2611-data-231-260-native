"""RFC 6901 JSON Pointer parsing and resolution.

Pointers are split on ``/`` first and then each token is decoded with ``~1``
replaced by ``/`` *before* ``~0`` is replaced by ``~`` (RFC 6901 section 4).
Doing the replacements the other way round would turn the token ``~01`` into
``/`` instead of ``~1``.  The empty string is a valid Pointer and addresses the
whole document.
"""

from __future__ import annotations

from typing import Any, List

from .errors import InvalidIndex, InvalidPointer


def parse_pointer(pointer: str) -> List[str]:
    """Decode an RFC 6901 JSON Pointer into a list of reference tokens.

    ``""`` -> ``[]`` (the whole document), ``"/"`` -> ``[""]`` (the member with
    the empty-string name).
    """
    if not isinstance(pointer, str):
        raise InvalidPointer("JSON Pointer must be a string", path=repr(pointer))
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise InvalidPointer(
            f"JSON Pointer must be empty or start with '/': {pointer!r}",
            path=pointer,
        )

    tokens: List[str] = []
    for raw in pointer[1:].split("/"):
        if "~" in raw:
            # Validate escapes before decoding: "~2" and a trailing "~" are not
            # legal RFC 6901 escapes.
            i = 0
            while i < len(raw):
                if raw[i] == "~":
                    if i + 1 >= len(raw) or raw[i + 1] not in "01":
                        raise InvalidPointer(
                            f"invalid '~' escape in JSON Pointer token {raw!r}",
                            path=pointer,
                        )
                    i += 2
                else:
                    i += 1
        tokens.append(raw.replace("~1", "/").replace("~0", "~"))
    return tokens


def format_pointer(tokens: List[str]) -> str:
    """Encode reference tokens back into a JSON Pointer (used for reporting)."""
    if not tokens:
        return ""
    return "".join(
        "/" + token.replace("~", "~0").replace("/", "~1") for token in tokens
    )


def parse_index(token: str, pointer: str) -> int:
    """Parse an array index.

    Only ``0`` and ASCII decimal integers without a leading zero are legal.
    ``-`` is never an index for remove/replace/test/from; ``add`` and the
    destination of ``copy``/``move`` handle a trailing ``-`` separately.
    """
    if token == "0":
        return 0
    if not token:
        raise InvalidIndex("array index must not be empty", path=pointer)
    if token[0] == "-":
        raise InvalidIndex(
            f"'-' is not a valid array index here: {pointer!r}", path=pointer
        )
    if token[0] == "0":
        raise InvalidIndex(
            f"array index must not have a leading zero: {token!r}", path=pointer
        )
    if not (token.isascii() and token.isdigit()):
        raise InvalidIndex(f"invalid array index {token!r}", path=pointer)
    return int(token)


def resolve(document: Any, tokens: List[str], pointer: str) -> Any:
    """Walk ``tokens`` from ``document``, returning the referenced value.

    A missing member, an index outside the array, or traversal through a
    scalar all raise ``InvalidPointer``.  ``null`` values are real values and
    are never treated as "missing".
    """
    current = document
    for depth, token in enumerate(tokens):
        if isinstance(current, dict):
            if token not in current:
                raise InvalidPointer(
                    f"object member {token!r} does not exist", path=pointer
                )
            current = current[token]
        elif isinstance(current, list):
            index = parse_index(token, pointer)
            if index >= len(current):
                raise InvalidPointer(
                    f"array index {index} is out of range (length {len(current)})",
                    path=pointer,
                )
            current = current[index]
        else:
            prefix = format_pointer(tokens[:depth]) or "<root>"
            raise InvalidPointer(
                f"cannot traverse into non-container value at {prefix!r}",
                path=pointer,
            )
    return current


def is_strict_prefix(prefix: List[str], tokens: List[str]) -> bool:
    """True when ``prefix`` is a proper, token-wise prefix of ``tokens``."""
    return len(prefix) < len(tokens) and tokens[: len(prefix)] == prefix
