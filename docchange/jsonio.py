"""Reject constants that are not valid JSON.

Python's ``json`` module accepts ``NaN``, ``Infinity`` and ``-Infinity`` even
though they are not JSON.  The fixtures contract promises finite numbers only,
so a document containing those tokens is a hard error rather than silently
importing a non-JSON value.
"""

from __future__ import annotations

import json
from typing import Any


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON numeric constant {name!r} is not allowed")


def loads(text: str) -> Any:
    """Parse JSON, rejecting NaN/Infinity and non-finite overflow."""
    return json.loads(text, parse_constant=_reject_constant)


def load(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return loads(handle.read())


def check_finite(value: Any, where: str = "document") -> None:
    """Raise ``ValueError`` if ``value`` contains a non-finite float."""
    from .engine import validate_json_value
    from .errors import InvalidOperation

    try:
        validate_json_value(value, where=where)
    except InvalidOperation as exc:
        raise ValueError(exc.message) from None
