"""Error types for the document-change ingester.

Every failure carries enough context for the batch log: a stable reason code,
the offending JSON Pointer (when one exists) and a human-readable message.
"""

from __future__ import annotations

from typing import Optional


class PatchError(Exception):
    """A single patch operation could not be applied.

    ``reason`` is a short machine-readable code.  ``path`` is the JSON Pointer
    involved in the failure (``None`` when the problem is not tied to a Pointer,
    e.g. a malformed ``ops`` array).  ``detail`` may name a secondary Pointer,
    such as ``from`` for a rejected ``move``.
    """

    reason = "patch_error"

    def __init__(
        self,
        message: str,
        path: Optional[str] = None,
        detail: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.path = path
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.message!r}, path={self.path!r})"


class InvalidPointer(PatchError):
    """The Pointer is syntactically invalid or cannot be resolved."""

    reason = "invalid_pointer"


class MissingTarget(PatchError):
    """A remove/replace/test/move target does not exist."""

    reason = "missing_target"


class InvalidOperation(PatchError):
    """Unknown ``op``, missing/non-string required members, non-JSON values."""

    reason = "invalid_operation"


class InvalidIndex(PatchError):
    """An array index is malformed (negative, leading zero, ...)."""

    reason = "invalid_index"


class ArrayIndexOutOfRange(PatchError):
    """A syntactically valid array index lies outside the array."""

    reason = "index_out_of_range"


class ParentNotContainer(PatchError):
    """The parent of the target is neither an object nor an array."""

    reason = "parent_not_container"


class MoveIntoDescendant(PatchError):
    """A move would place a value inside its own descendant subtree."""

    reason = "move_into_descendant"


class RootRemovalUnsupported(PatchError):
    """This ingester deliberately does not support removing the root."""

    reason = "root_removal_unsupported"


class TestFailed(PatchError):
    """A ``test`` operation did not hold."""

    reason = "test_failed"


class InvalidBatch(PatchError):
    """The batch record itself is malformed."""

    reason = "invalid_batch"


class UnknownDocument(PatchError):
    """The batch names a doc_id that is not present in the store."""

    reason = "unknown_doc"
