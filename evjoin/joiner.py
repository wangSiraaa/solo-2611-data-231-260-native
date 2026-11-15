"""Streaming dual-input event-time LEFT JOIN.

Semantics
---------
For a left ``l`` and a right ``r``::

    l.key == r.key and l.ts + lower_ms <= r.ts < l.ts + upper_ms

Both streams carry a monotonically non-decreasing watermark.  A watermark
``W`` on a side promises that every future *legal* record on that side has
``ts >= W``.  Consequently a record with ``ts < W`` is late: it is diverted to
``late`` and never participates in the join.  ``ts == W`` is still legal.

EOF is not an end-of-stream marker.  A side is finite only after an explicit
``end`` event; at that point its effective watermark is ``+inf``.

Retirement (the two sides intentionally use *different* thresholds)
-------------------------------------------------------------------
Left buffer
    A buffered left ``l`` can only match a future right with
    ``r.ts >= W_R``.  Its match interval is
    ``[l.ts + lower, l.ts + upper)``.  Once ``W_R >= l.ts + upper`` no future
    right can fall inside the interval, so ``l`` is safely retired.  If it
    never matched, the final ``right_id = null`` fact is emitted at exactly
    that moment.

Right buffer
    A buffered right ``r`` can only match a future left with ``l.ts >= W_L``.
    Rewriting the match predicate gives ``r.ts - upper < l.ts <= r.ts - lower``.
    Once ``W_L > r.ts - lower`` the smallest possible future left is already
    past the largest possible matching left, so ``r`` is safely retired.
    Equality is *not* enough: a left with ``ts == W_L == r.ts - lower`` still
    matches.

There is no shared "min watermark" threshold: lefts are retired by the *right*
watermark, rights by the *left* watermark, with the two formulas above.

Only in-memory state is kept; there is no checkpointing/recovery.
"""

from __future__ import annotations

import heapq
import json
import math
from dataclasses import dataclass
from typing import Any, Iterable, Optional

SIDES = ("L", "R")


class JoinError(Exception):
    """Invalid input or incomplete stream.

    ``line`` is the 1-based physical input line when known.
    """

    def __init__(self, message: str, line: Optional[int] = None) -> None:
        self.message = message
        self.line = line
        super().__init__(f"line {line}: {message}" if line is not None else message)


def _is_int(value: Any) -> bool:
    # bool is a subclass of int but is not an acceptable timestamp.
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical(value: Any) -> str:
    """Canonical, type-stable JSON token used for key/id grouping."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fmt_watermark(value: float) -> Any:
    if value == math.inf:
        return "+inf"
    if value == -math.inf:
        return "-inf"
    return value


@dataclass(eq=False)
class _Record:
    side: str
    id: Any
    key: Any
    key_token: str
    ts: int
    value: Any
    seq: int          # 1-based push() step
    line: int         # 1-based physical input line
    matched: bool = False


class Joiner:
    """Push-based event-time LEFT JOIN operator.

    ``push(event)`` consumes one input event and returns the list of match /
    final-unmatched facts newly produced by that event.  ``finish()`` may only
    be called after both sides have explicitly ended.
    """

    def __init__(self, lower_ms: int, upper_ms: int) -> None:
        if not _is_int(lower_ms) or not _is_int(upper_ms):
            raise ValueError("lower_ms and upper_ms must be integers")
        self.lower_ms = lower_ms
        self.upper_ms = upper_ms

        # -inf until the first watermark; end makes the effective value +inf.
        self.watermark: dict[str, float] = {"L": -math.inf, "R": -math.inf}
        self.ended: dict[str, bool] = {"L": False, "R": False}

        # Per-side, per-key buffers; each list stays in arrival order.
        self._lefts: dict[str, list[_Record]] = {}
        self._rights: dict[str, list[_Record]] = {}
        # Heaps order buffered records by ts for timely retirement.
        self._left_heap: list[tuple[int, int, _Record]] = []
        self._right_heap: list[tuple[int, int, _Record]] = []

        self._seen_ids: set[str] = set()
        self.step = 0
        self.finished = False

        self.facts: list[dict[str, Any]] = []   # all match + unmatched facts
        self.matches: list[dict[str, Any]] = []  # facts with right_id != null
        self.late: list[dict[str, Any]] = []
        self.trace: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ ctors
    @classmethod
    def from_config(cls, path: str) -> "Joiner":
        try:
            with open(path, "r", encoding="utf-8") as fh:
                config = json.load(fh)
        except OSError as exc:
            raise JoinError(f"cannot read config {path!r}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise JoinError(f"invalid config JSON in {path!r}: {exc}") from exc
        return cls.from_config_dict(config)

    @classmethod
    def from_config_dict(cls, config: Any) -> "Joiner":
        if not isinstance(config, dict):
            raise JoinError("config must be a JSON object")
        if "lower_ms" not in config or "upper_ms" not in config:
            raise JoinError("config needs lower_ms and upper_ms")
        return cls(config["lower_ms"], config["upper_ms"])

    # ------------------------------------------------------------------- API
    def push(self, event: Any, *, line_no: Optional[int] = None) -> list[dict[str, Any]]:
        """Consume one event, return only the facts produced by this step.

        ``event`` may be a dict or a JSON object string.  ``line_no`` is the
        original 1-based line number used in diagnostics/late records; it
        defaults to the push step number.
        """
        if self.finished:
            raise JoinError("push() called after finish()", line_no)

        self.step += 1
        seq = self.step
        line = line_no if line_no is not None else seq

        if isinstance(event, str):
            try:
                event = json.loads(event)
            except json.JSONDecodeError as exc:
                raise JoinError(f"invalid JSON: {exc}", line) from exc
        if not isinstance(event, dict):
            raise JoinError("record must be a JSON object", line)

        before = self._watermark_snapshot()
        outputs: list[dict[str, Any]] = []
        late_now: list[dict[str, Any]] = []

        op = event.get("op")
        if op == "data":
            self._on_data(event, seq, line, outputs, late_now)
        elif op == "watermark":
            self._on_watermark(event, line, outputs)
        elif op == "end":
            self._on_end(event, line, outputs)
        else:
            raise JoinError(f"unknown or missing op: {op!r}", line)

        # Thresholds only change on watermark/end, but running this here keeps
        # the invariant "nothing retirable is retained" unconditional.
        self._retire(outputs)

        self.trace.append(
            {
                "step": seq,
                "line": line,
                "input": event,
                "watermarks_before": before,
                "watermarks": self._watermark_snapshot(),
                "ended": dict(self.ended),
                "retained": self.retained_ids(),
                "outputs": outputs,
                "late": late_now,
            }
        )
        return outputs

    def finish(self) -> list[dict[str, Any]]:
        """Finalize after *both* explicit end events; otherwise raise."""
        if self.finished:
            return []
        missing = [side for side in SIDES if not self.ended[side]]
        if missing:
            raise JoinError(
                "incomplete: side(s) "
                + ", ".join(missing)
                + " not ended (EOF is not end)"
            )
        outputs: list[dict[str, Any]] = []
        self._retire(outputs)
        self.finished = True
        return outputs

    # ------------------------------------------------------------- accessors
    def retained_ids(self) -> dict[str, list[Any]]:
        """Ids currently buffered, in arrival order."""
        return {"L": self._retained("L"), "R": self._retained("R")}

    def _retained(self, side: str) -> list[Any]:
        buffers = self._lefts if side == "L" else self._rights
        records = [rec for bucket in buffers.values() for rec in bucket]
        records.sort(key=lambda rec: rec.seq)
        return [rec.id for rec in records]

    def _watermark_snapshot(self) -> dict[str, Any]:
        return {
            side: ("+inf" if self.ended[side] else _fmt_watermark(self.watermark[side]))
            for side in SIDES
        }

    def effective_watermark(self, side: str) -> float:
        return math.inf if self.ended[side] else self.watermark[side]

    # -------------------------------------------------------------- handlers
    def _on_data(
        self,
        event: dict[str, Any],
        seq: int,
        line: int,
        outputs: list[dict[str, Any]],
        late_now: list[dict[str, Any]],
    ) -> None:
        side = self._require_side(event, line)
        if self.ended[side]:
            raise JoinError(f"data received on side {side} after end", line)

        if "id" not in event:
            raise JoinError("data record needs id", line)
        event_id = event["id"]
        if isinstance(event_id, bool) or not isinstance(event_id, (str, int)):
            raise JoinError("id must be a string or integer", line)
        if isinstance(event_id, str) and event_id == "":
            raise JoinError("id must not be empty", line)
        id_token = _canonical(event_id)
        if id_token in self._seen_ids:
            raise JoinError(f"duplicate id {event_id!r}", line)
        self._seen_ids.add(id_token)

        if "key" not in event:
            raise JoinError("data record needs key", line)
        if "value" not in event:
            raise JoinError("data record needs value", line)
        key = event["key"]
        key_token = _canonical(key)
        ts = event.get("ts")
        if not _is_int(ts):
            raise JoinError("ts must be an integer number of milliseconds", line)
        value = event["value"]

        # Judge with the watermark that existed *before* this event arrived.
        if ts < self.watermark[side]:
            late = {
                "line": line,
                "side": side,
                "id": event_id,
                "key": key,
                "ts": ts,
                "value": value,
                "watermark": self.watermark[side],
            }
            self.late.append(late)
            late_now.append(late)
            return

        rec = _Record(
            side=side,
            id=event_id,
            key=key,
            key_token=key_token,
            ts=ts,
            value=value,
            seq=seq,
            line=line,
        )

        if side == "L":
            self._handle_left(rec, outputs)
        else:
            self._handle_right(rec, outputs)

    def _handle_left(self, rec: _Record, outputs: list[dict[str, Any]]) -> None:
        matched_any = False
        for right in self._rights.get(rec.key_token, ()):
            if rec.ts + self.lower_ms <= right.ts < rec.ts + self.upper_ms:
                outputs.append(self._emit_match(rec, right))
                rec.matched = True
                matched_any = True

        # If the right watermark has already closed this left's whole interval,
        # it is final now: a matched left has no null fact, an unmatched one does.
        if self.effective_watermark("R") >= rec.ts + self.upper_ms:
            if not matched_any:
                outputs.append(self._emit_unmatched(rec))
            return

        bucket = self._lefts.setdefault(rec.key_token, [])
        bucket.append(rec)
        heapq.heappush(self._left_heap, (rec.ts, rec.seq, rec))

    def _handle_right(self, rec: _Record, outputs: list[dict[str, Any]]) -> None:
        for left in self._lefts.get(rec.key_token, ()):
            if left.ts + self.lower_ms <= rec.ts < left.ts + self.upper_ms:
                outputs.append(self._emit_match(left, rec))
                left.matched = True

        # Left watermark has already passed the largest left ts that could
        # match this right; no future left can join with it.
        if self.effective_watermark("L") > rec.ts - self.lower_ms:
            return

        bucket = self._rights.setdefault(rec.key_token, [])
        bucket.append(rec)
        heapq.heappush(self._right_heap, (rec.ts, rec.seq, rec))

    def _on_watermark(
        self, event: dict[str, Any], line: int, outputs: list[dict[str, Any]]
    ) -> None:
        side = self._require_side(event, line)
        if self.ended[side]:
            raise JoinError(f"watermark received on side {side} after end", line)
        ts = event.get("ts")
        if not _is_int(ts):
            raise JoinError("watermark ts must be an integer", line)
        if ts < self.watermark[side]:
            raise JoinError(
                f"watermark regression on side {side}: "
                f"{ts} < {self.watermark[side]}",
                line,
            )
        # Equality is allowed: watermarks may stay flat.
        self.watermark[side] = ts
        self._retire(outputs)

    def _on_end(
        self, event: dict[str, Any], line: int, outputs: list[dict[str, Any]]
    ) -> None:
        side = self._require_side(event, line)
        if self.ended[side]:
            raise JoinError(f"side {side} already ended", line)
        self.ended[side] = True
        # end is the explicit +inf watermark for this side.
        self._retire(outputs)

    # ------------------------------------------------------------- retirement
    def _retire(self, outputs: list[dict[str, Any]]) -> None:
        right_wm = self.effective_watermark("R")
        while self._left_heap and self._left_heap[0][0] + self.upper_ms <= right_wm:
            _, _, rec = heapq.heappop(self._left_heap)
            self._unbuffer("L", rec)
            if not rec.matched:
                outputs.append(self._emit_unmatched(rec))

        left_wm = self.effective_watermark("L")
        while self._right_heap and self._right_heap[0][0] - self.lower_ms < left_wm:
            _, _, rec = heapq.heappop(self._right_heap)
            self._unbuffer("R", rec)

    def _unbuffer(self, side: str, rec: _Record) -> None:
        buffers = self._lefts if side == "L" else self._rights
        bucket = buffers.get(rec.key_token)
        if bucket is not None:
            try:
                bucket.remove(rec)
            except ValueError:  # defensive; should not happen
                pass
            if not bucket:
                del buffers[rec.key_token]

    # ----------------------------------------------------------------- facts
    def _emit_match(self, left: _Record, right: _Record) -> dict[str, Any]:
        fact = {
            "left_id": left.id,
            "right_id": right.id,
            "left_ts": left.ts,
            "right_ts": right.ts,
            "left_value": left.value,
            "right_value": right.value,
        }
        self.facts.append(fact)
        self.matches.append(fact)
        return fact

    def _emit_unmatched(self, left: _Record) -> dict[str, Any]:
        fact = {
            "left_id": left.id,
            "right_id": None,
            "left_ts": left.ts,
            "right_ts": None,
            "left_value": left.value,
            "right_value": None,
        }
        self.facts.append(fact)
        return fact

    # ------------------------------------------------------------ validation
    @staticmethod
    def _require_side(event: dict[str, Any], line: int) -> str:
        side = event.get("side")
        if side not in SIDES:
            raise JoinError("side must be 'L' or 'R'", line)
        return side


def sort_facts(facts: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable deterministic export order: by left id, then right id.

    Unmatched facts (``right_id is None``) sort before matched facts for the
    same left, and the underlying ``sort`` is stable so equal keys keep the
    order in which they were produced.
    """

    def key(fact: dict[str, Any]) -> tuple[str, int, str]:
        right_id = fact.get("right_id")
        return (
            _canonical(fact.get("left_id")),
            0 if right_id is None else 1,
            "" if right_id is None else _canonical(right_id),
        )

    return sorted(facts, key=key)


__all__ = ["Joiner", "JoinError", "sort_facts"]
