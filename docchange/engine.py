"""RFC 6902 patch execution core.

Design constraints from the project brief:

* the empty Pointer addresses the whole document;
* ``~1`` is decoded before ``~0``;
* array indexes are only ``0`` or decimal without a leading zero; the append
  token ``-`` is accepted where add semantics apply (``add``, ``copy``'s
  destination, ``move``'s destination) and rejected everywhere else;
* ``add`` replaces an existing object member but inserts into an array;
* ``move`` removes first, then resolves the destination against the *modified*
  document (no index fix-up using the original position);
* a value may not be moved into its own descendant, but moving to the same
  Pointer is a successful no-op;
* ``copy`` is a deep copy;
* removing the root is not supported and is rejected;
* a batch is atomic: if any operation fails, the pre-batch document is kept.

The module operates on plain Python values decoded from JSON.  Inputs are
never mutated in public entry points.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, List, Mapping, MutableMapping, Optional, Sequence

from .compare import json_equal
from .errors import (
    ArrayIndexOutOfRange,
    InvalidBatch,
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
from .pointer import (
    format_pointer,
    is_strict_prefix,
    parse_index,
    parse_pointer,
)


# ---------------------------------------------------------------------------
# Value validation helpers
# -------------------------------------------------------------------------


def validate_json_value(value: Any, where: str = "value") -> None:
    """Reject values outside the project's JSON contract.

    The parser rejects the literal tokens ``NaN``/``Infinity``, but overflow
    such as ``1e400`` still decodes to ``inf``; that must not enter a document.
    """
    stack = [value]
    while stack:
        current = stack.pop()
        if current is None or isinstance(current, bool) or isinstance(current, str):
            continue
        if isinstance(current, int):
            continue
        if isinstance(current, float):
            if current != current or current in (float("inf"), float("-inf")):
                raise InvalidOperation(
                    f"non-finite number is not allowed ({where})"
                )
            continue
        if isinstance(current, list):
            stack.extend(current)
            continue
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str):
                    raise InvalidOperation(
                        f"object keys must be strings ({where})"
                    )
                stack.append(item)
            continue
        raise InvalidOperation(
            f"unsupported value type {type(current).__name__} ({where})"
        )


# ---------------------------------------------------------------------------


def _require_str_member(
    op: Mapping[str, Any], name: str, op_name: str, path: Optional[str] = None
) -> str:
    if name not in op:
        raise InvalidOperation(
            f"{op_name!r} operation is missing required member {name!r}",
            path=path,
        )
    value = op[name]
    if not isinstance(value, str):
        raise InvalidOperation(
            f"{op_name!r} operation member {name!r} must be a string",
            path=path,
        )
    return value


def _require_value(op: Mapping[str, Any], op_name: str, path: str) -> Any:
    if "value" not in op:
        raise InvalidOperation(
            f"{op_name!r} operation is missing required member 'value'",
            path=path,
        )
    value = op["value"]
    try:
        validate_json_value(value, where=f"{op_name} value at {path!r}")
    except InvalidOperation as exc:
        raise InvalidOperation(exc.message, path=path) from None
    return copy.deepcopy(value)


def _resolve_target(document: Any, tokens: List[str], pointer: str) -> Any:
    """Resolve a Pointer whose target must exist."""
    current = document
    for depth, token in enumerate(tokens):
        if isinstance(current, dict):
            if token not in current:
                raise MissingTarget(
                    f"object member {token!r} does not exist", path=pointer
                )
            current = current[token]
        elif isinstance(current, list):
            index = parse_index(token, pointer)
            if index >= len(current):
                raise ArrayIndexOutOfRange(
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


def _resolve_parent(
    document: Any, tokens: List[str], pointer: str, *, missing_reason: str
) -> tuple[Any, str]:
    """Resolve the parent container for a non-root Pointer.

    ``missing_reason`` selects ``MissingTarget`` (existing-target operations)
    or ``InvalidPointer`` (``add``, whose parent must already exist but whose
    final member/index need not).
    """
    if not tokens:
        raise InvalidPointer("root has no parent", path=pointer)
    current = document
    for depth, token in enumerate(tokens[:-1]):
        if isinstance(current, dict):
            if token not in current:
                message = f"object member {token!r} does not exist"
                if missing_reason == "missing_target":
                    raise MissingTarget(message, path=pointer)
                raise InvalidPointer(message, path=pointer)
            current = current[token]
        elif isinstance(current, list):
            index = parse_index(token, pointer)
            if index >= len(current):
                message = (
                    f"array index {index} is out of range (length {len(current)})"
                )
                if missing_reason == "missing_target":
                    raise ArrayIndexOutOfRange(message, path=pointer)
                raise InvalidPointer(message, path=pointer)
            current = current[index]
        else:
            prefix = format_pointer(tokens[:depth]) or "<root>"
            raise InvalidPointer(
                f"cannot traverse into non-container value at {prefix!r}",
                path=pointer,
            )
    if not isinstance(current, (dict, list)):
        raise ParentNotContainer(
            f"parent of {pointer!r} is not an object or array", path=pointer
        )
    return current, tokens[-1]


def _insert(document: Any, tokens: List[str], pointer: str, value: Any) -> Any:
    """Implement ``add``: returns the (possibly new) document root.

    ``copy`` and the destination half of ``move`` use the same rules, so a
    trailing ``-`` means "append" there too.  ``remove``/``replace``/``test``
    and every ``from`` Pointer never reach this helper and therefore reject
    ``-``.
    """
    if not tokens:
        return value
    parent, token = _resolve_parent(
        document, tokens, pointer, missing_reason="invalid_pointer"
    )
    if isinstance(parent, dict):
        # A "-" member of an object is an ordinary key; only arrays treat it
        # specially.
        parent[token] = value
        return document
    # parent is an array
    if token == "-":
        parent.append(value)
        return document
    index = parse_index(token, pointer)
    if index > len(parent):
        raise ArrayIndexOutOfRange(
            f"array index {index} is out of range for insertion (length {len(parent)})",
            path=pointer,
        )
    parent.insert(index, value)
    return document


def _remove_at(document: Any, tokens: List[str], pointer: str) -> None:
    if not tokens:
        raise RootRemovalUnsupported(
            "removing the root document is not supported", path=pointer
        )
    parent, token = _resolve_parent(
        document, tokens, pointer, missing_reason="missing_target"
    )
    if isinstance(parent, dict):
        if token not in parent:
            raise MissingTarget(f"object member {token!r} does not exist", path=pointer)
        del parent[token]
        return
    index = parse_index(token, pointer)
    if index >= len(parent):
        raise ArrayIndexOutOfRange(
            f"array index {index} is out of range (length {len(parent)})",
            path=pointer,
        )
    del parent[index]


def _replace_at(document: Any, tokens: List[str], pointer: str, value: Any) -> Any:
    if not tokens:
        return value
    parent, token = _resolve_parent(
        document, tokens, pointer, missing_reason="missing_target"
    )
    if isinstance(parent, dict):
        if token not in parent:
            raise MissingTarget(f"object member {token!r} does not exist", path=pointer)
        parent[token] = value
        return document
    index = parse_index(token, pointer)
    if index >= len(parent):
        raise ArrayIndexOutOfRange(
            f"array index {index} is out of range (length {len(parent)})",
            path=pointer,
        )
    parent[index] = value
    return document


def _op_add(document: Any, op: Mapping[str, Any], path: str) -> Any:
    tokens = parse_pointer(path)
    value = _require_value(op, "add", path)
    return _insert(document, tokens, path, value)


def _op_remove(document: Any, op: Mapping[str, Any], path: str) -> Any:
    tokens = parse_pointer(path)
    _remove_at(document, tokens, path)
    return document


def _op_replace(document: Any, op: Mapping[str, Any], path: str) -> Any:
    tokens = parse_pointer(path)
    value = _require_value(op, "replace", path)
    return _replace_at(document, tokens, path, value)


def _op_move(document: Any, op: Mapping[str, Any], path: str) -> Any:
    from_path = _require_str_member(op, "from", "move", path)
    from_tokens = parse_pointer(from_path)
    path_tokens = parse_pointer(path)

    if is_strict_prefix(from_tokens, path_tokens):
        raise MoveIntoDescendant(
            f"cannot move {from_path!r} into its own descendant {path!r}",
            path=path,
            detail=from_path,
        )

    # The source must exist even for a same-Pointer no-op (RFC 6902 requires
    # the "from" location to exist).  Resolving the root returns the document.
    value = _resolve_target(document, from_tokens, from_path)

    if from_tokens == path_tokens:
        return document

    if not from_tokens:
        # Unreachable while the descendant rule above holds, but the root is
        # explicitly not removable in this project.
        raise RootRemovalUnsupported(
            "removing the root document is not supported", path=from_path
        )

    _remove_at(document, from_tokens, from_path)
    # The destination is interpreted against the document *after* the removal;
    # the value itself was captured before it, so no re-resolution is needed.
    return _insert(document, path_tokens, path, value)


def _op_copy(document: Any, op: Mapping[str, Any], path: str) -> Any:
    from_path = _require_str_member(op, "from", "copy", path)
    from_tokens = parse_pointer(from_path)
    path_tokens = parse_pointer(path)
    value = copy.deepcopy(_resolve_target(document, from_tokens, from_path))
    # ``copy`` uses add semantics for its destination, including a trailing
    # ``-`` on an array (append).  ``-`` remains illegal for remove, replace,
    # test and every ``from`` Pointer.
    return _insert(document, path_tokens, path, value)


def _op_test(document: Any, op: Mapping[str, Any], path: str) -> Any:
    tokens = parse_pointer(path)
    expected = _require_value(op, "test", path)
    actual = _resolve_target(document, tokens, path)
    if not json_equal(actual, expected):
        raise TestFailed(
            "test failed: document value does not structurally match the expected value",
            path=path,
        )
    return document


_HANDLERS = {
    "add": _op_add,
    "remove": _op_remove,
    "replace": _op_replace,
    "move": _op_move,
    "copy": _op_copy,
    "test": _op_test,
}


def _apply_one(document: Any, op: Any) -> Any:
    if not isinstance(op, dict):
        raise InvalidOperation("each operation must be a JSON object")
    op_name = op.get("op")
    if not isinstance(op_name, str):
        raise InvalidOperation("operation is missing a string 'op' member")
    if op_name not in _HANDLERS:
        # Report the path (when it is usable) alongside the bad operation name.
        reported_path = op.get("path") if isinstance(op.get("path"), str) else None
        raise InvalidOperation(
            f"unknown operation {op_name!r}", path=reported_path
        )
    path = _require_str_member(op, "path", op_name)
    return _HANDLERS[op_name](document, op, path)


def apply_ops(document: Any, ops: Sequence[Any]) -> Any:
    """Apply ``ops`` to a deep copy of ``document`` and return the result.

    On failure ``PatchError`` is raised with ``op_index`` and ``op_name``
    attributes attached; the input ``document`` is never modified.
    """
    if not isinstance(ops, list):
        raise InvalidOperation("'ops' must be a list of operations")
    working = copy.deepcopy(document)
    for op_index, op in enumerate(ops):
        try:
            working = _apply_one(working, op)
        except PatchError as exc:
            exc.op_index = op_index
            exc.op_name = op.get("op") if isinstance(op, dict) else None
            raise
    return working


# ---------------------------------------------------------------------------
# Batch-level atomic execution
# ---------------------------------------------------------------------------


def summarize(document: Any) -> dict:
    """A stable digest of a document, used in the batch log.

    The digest is insensitive to object member order (keys are sorted) but
    sensitive to array order, numeric type (``1`` vs ``1.0``), and string
    contents, which is enough to tell whether a batch changed the document.
    """
    encoded = json.dumps(
        document,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if isinstance(document, dict):
        kind, size = "object", len(document)
    elif isinstance(document, list):
        kind, size = "array", len(document)
    elif document is None:
        kind, size = "null", 0
    else:
        kind, size = type(document).__name__, 0
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "bytes": len(encoded),
        "type": kind,
        "size": size,
    }


def _record(
    batch_id: Any,
    doc_id: Any,
    *,
    status: str,
    op_index: Optional[int] = None,
    op_name: Optional[str] = None,
    path: Optional[str] = None,
    from_path: Optional[str] = None,
    reason: Optional[str] = None,
    message: Optional[str] = None,
    summary_before: Optional[dict] = None,
    summary_after: Optional[dict] = None,
    **extra: Any,
) -> dict:
    record = {
        "batch_id": batch_id,
        "doc_id": doc_id,
        "status": status,
        "op_index": op_index,
        "op": op_name if isinstance(op_name, str) else None,
        "path": path,
        "from": from_path,
        "reason": reason,
        "message": message,
        "summary_before": summary_before,
        "summary_after": summary_after,
    }
    record.update(extra)
    return record


def _json_locator(value: Any) -> Any:
    """Return a value usable in the log as a batch/document locator.

    Valid locators are strings or integers; anything else (including booleans,
    which Python sees as integers) is reported as ``None`` so that a malformed
    input record can still be serialized and logged.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def apply_batch(documents: MutableMapping[str, Any], batch: Any) -> dict:
    """Apply one batch atomically to ``documents``.

    On acceptance the store is updated in place.  On rejection the store is
    left exactly as it was and the returned summary is the pre-batch summary
    (also supplied as ``summary_before``).

    Returns a JSON-serializable record describing the outcome.
    """
    batch_id = batch.get("batch_id") if isinstance(batch, dict) else None
    doc_id = batch.get("doc_id") if isinstance(batch, dict) else None
    # Logged identifiers must be JSON-serializable even for malformed input,
    # so a dict/list `batch_id` or a non-string `doc_id` is logged as null.
    logged_batch_id = _json_locator(batch_id)
    logged_doc_id = doc_id if isinstance(doc_id, str) else None

    if not isinstance(batch, dict):
        return _record(
            logged_batch_id, logged_doc_id, status="rejected",
            reason=InvalidBatch.reason,
            message="batch must be a JSON object",
        )

    if "batch_id" not in batch:
        return _record(
            logged_batch_id, logged_doc_id, status="rejected",
            reason=InvalidBatch.reason,
            message="batch is missing 'batch_id'",
        )
    if (
        batch_id is None
        or not isinstance(batch_id, (str, int))
        or isinstance(batch_id, bool)
    ):
        return _record(
            logged_batch_id, logged_doc_id, status="rejected",
            reason=InvalidBatch.reason,
            message="'batch_id' must be a string or integer",
        )
    if not isinstance(doc_id, str):
        return _record(
            logged_batch_id, logged_doc_id, status="rejected",
            reason=InvalidBatch.reason,
            message="'doc_id' must be a string",
        )
    if "ops" not in batch or not isinstance(batch["ops"], list):
        return _record(
            logged_batch_id, logged_doc_id, status="rejected",
            reason=InvalidBatch.reason,
            message="'ops' must be a list of operations",
        )

    if doc_id not in documents:
        return _record(
            logged_batch_id, logged_doc_id, status="rejected",
            reason=UnknownDocument.reason,
            message=f"unknown document {doc_id!r}",
        )

    before = summarize(documents[doc_id])
    ops = batch["ops"]

    try:
        working = copy.deepcopy(documents[doc_id])
        for op_index, op in enumerate(ops):
            try:
                working = _apply_one(working, op)
            except PatchError as exc:
                raise _with_op_context(exc, op_index, op) from None
    except PatchError as exc:
        return _record(
            logged_batch_id,
            logged_doc_id,
            status="rejected",
            op_index=getattr(exc, "op_index", None),
            op_name=getattr(exc, "op_name", None),
            path=exc.path,
            from_path=exc.detail,
            reason=exc.reason,
            message=exc.message,
            summary_before=before,
            # Rejection does not touch the store, so this equals the pre-state.
            summary_after=before,
        )

    documents[doc_id] = working
    return _record(
        logged_batch_id,
        logged_doc_id,
        status="accepted",
        summary_before=before,
        summary_after=summarize(working),
    )


def _with_op_context(exc: PatchError, op_index: int, op: Any) -> PatchError:
    exc.op_index = op_index
    exc.op_name = op.get("op") if isinstance(op, dict) else None
    return exc
