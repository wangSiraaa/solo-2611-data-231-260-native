"""Error types for the fidelity patcher.

Every rejection is a :class:`PatchError` carrying a machine-readable
``code``.  Nothing is written unless the whole batch validates.
"""

from __future__ import annotations

from enum import Enum


class PatchErrorCode(str, Enum):
    # -- input / patch shape -------------------------------------------------
    INVALID_UTF8 = "INVALID_UTF8"
    INVALID_PATCH = "INVALID_PATCH"
    SHA256_MISMATCH = "SHA256_MISMATCH"
    EMPTY_DOCUMENT = "EMPTY_DOCUMENT"
    # -- CSV lexical errors --------------------------------------------------
    BARE_QUOTE = "BARE_QUOTE"
    UNCLOSED_QUOTE = "UNCLOSED_QUOTE"
    TRAILING_AFTER_QUOTE = "TRAILING_AFTER_QUOTE"
    COLUMN_COUNT_MISMATCH = "COLUMN_COUNT_MISMATCH"
    # -- patch semantic errors ----------------------------------------------
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    ROW_OUT_OF_RANGE = "ROW_OUT_OF_RANGE"
    WRONG_OLD_VALUE = "WRONG_OLD_VALUE"
    DUPLICATE_CELL = "DUPLICATE_CELL"
    # -- output environment --------------------------------------------------
    OUTPUT_IS_SOURCE = "OUTPUT_IS_SOURCE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class PatchError(Exception):
    """A batch-level failure; no partial output may be emitted."""

    def __init__(self, code: PatchErrorCode, message: str, **details):
        self.code = code
        self.message = message
        self.details = details
        super().__init__(f"{code.value}: {message}")

    def as_dict(self) -> dict:
        return {"error": self.code.value, "message": self.message, **self.details}
