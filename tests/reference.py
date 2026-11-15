"""Independent brute-force reference for the final join result.

This deliberately shares no code with :mod:`evjoin`.  It consumes the same
event order, applies the same arrival-time watermark rule (records with
``ts < current side watermark`` are late and excluded), then nested-loop joins
everything that survived.  It knows nothing about buffering or retirement, so
it is a useful oracle for the *final* fact set.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any, Iterable


def reference(events: Iterable[dict[str, Any]], lower_ms: int, upper_ms: int) -> dict[str, Any]:
    wm = {"L": -math.inf, "R": -math.inf}
    ended = {"L": False, "R": False}
    lefts: list[dict[str, Any]] = []
    rights: list[dict[str, Any]] = []
    late: list[Any] = []

    for event in events:
        op = event.get("op")
        side = event.get("side")
        if op == "data":
            if ended[side]:
                raise ValueError("data after end")
            if event["ts"] < wm[side]:
                late.append(event["id"])
                continue
            record = {k: event[k] for k in ("side", "id", "key", "ts", "value")}
            (lefts if side == "L" else rights).append(record)
        elif op == "watermark":
            if ended[side]:
                raise ValueError("watermark after end")
            if event["ts"] < wm[side]:
                raise ValueError("watermark regression")
            wm[side] = event["ts"]
        elif op == "end":
            if ended[side]:
                raise ValueError("double end")
            ended[side] = True
        else:
            raise ValueError(f"bad op {op!r}")

    matches: list[tuple[Any, Any]] = []
    matched_left = 0
    unmatched: list[tuple[Any, None]] = []
    for left in lefts:
        hit = False
        for right in rights:
            if (
                left["key"] == right["key"]
                and left["ts"] + lower_ms <= right["ts"] < left["ts"] + upper_ms
            ):
                matches.append((left["id"], right["id"]))
                hit = True
        if not hit:
            unmatched.append((left["id"], None))

    return {
        "facts": Counter(matches + unmatched),
        "late": Counter(late),
        "lefts": len(lefts),
        "rights": len(rights),
    }
