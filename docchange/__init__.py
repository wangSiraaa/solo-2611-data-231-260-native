"""Document-change ingester: atomic RFC 6902 batch application."""

from __future__ import annotations

from .compare import json_equal
from .engine import apply_batch, apply_ops, summarize, validate_json_value
from .errors import (
    ArrayIndexOutOfRange,
    InvalidBatch,
    InvalidIndex,
    InvalidOperation,
    InvalidPointer,
    MissingTarget,
    MoveIntoDescendant,
    ParentNotContainer,
    PatchError,
    RootRemovalUnsupported,
    TestFailed,
    UnknownDocument,
)
from .pointer import format_pointer, parse_pointer

__all__ = [
    "apply_batch",
    "apply_ops",
    "summarize",
    "validate_json_value",
    "json_equal",
    "parse_pointer",
    "format_pointer",
    "PatchError",
    "InvalidPointer",
    "MissingTarget",
    "InvalidOperation",
    "InvalidIndex",
    "ArrayIndexOutOfRange",
    "ParentNotContainer",
    "MoveIntoDescendant",
    "RootRemovalUnsupported",
    "TestFailed",
    "InvalidBatch",
    "UnknownDocument",
]
