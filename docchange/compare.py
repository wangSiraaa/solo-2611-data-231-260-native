"""JSON structural equality used by RFC 6902 ``test``.

The rules implemented here are deliberately *JSON* rules, not Python equality
rules:

* object member order is irrelevant;
* array element order matters;
* numbers compare numerically, so ``1 == 1.0``;
* booleans are not numbers, so ``true != 1``;
* strings are compared byte-for-byte (no Unicode normalization);
* ``null`` equals ``null`` and nothing else;
* ``null`` is a value, distinct from a missing member (missing is caught while
  resolving the Pointer, not here).
"""

from __future__ import annotations

import math
from typing import Any


def json_equal(left: Any, right: Any) -> bool:
    """Return True when two parsed JSON values are structurally equal."""
    # bool must be checked before int: in Python ``True`` is an ``int``.
    left_is_bool = isinstance(left, bool)
    right_is_bool = isinstance(right, bool)
    if left_is_bool or right_is_bool:
        return left_is_bool and right_is_bool and left == right

    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        if not (math.isfinite(left) and math.isfinite(right)):
            # Non-finite numbers are outside this project's input contract;
            # never report them equal (NaN would compare false anyway).
            return False
        # Python compares int/float numerically and exactly (no precision loss
        # for large integers the way a float conversion would introduce).
        return left == right

    if isinstance(left, str) or isinstance(right, str):
        return isinstance(left, str) and isinstance(right, str) and left == right

    if left is None or right is None:
        return left is None and right is None

    if isinstance(left, dict) and isinstance(right, dict):
        if left.keys() != right.keys():
            return False
        return all(json_equal(left[key], right[key]) for key in left)

    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return False
        return all(json_equal(a, b) for a, b in zip(left, right))

    return False
