"""Incremental, interleaving-safe matcher for contiguous partition segments.

Semantics implemented (see README):
  * A match is a *contiguous* block of rows from one partition; every row in
    the block is assigned to exactly one pattern variable (no internal skips).
  * Variables appear in the pattern's order; each is repeated ``min..max``
    times, and the same start position may admit several complete allocations.
    The greedy result is the lexicographically greatest repetition-count
    vector in variable order.
  * Matching at one start position is resolved as soon as the seen rows make
    every lexicographically greater allocation impossible.  Until then the raw
    rows are buffered; nothing is guessed about unseen events.
  * ``skip == "next_row"`` resumes at start+1, ``skip == "past_last"`` resumes
    after the matched block.  Both can yield different overlaps.
  * ``end(partition)`` closes the partition and immediately settles every
    complete match still representable by buffered rows.

A partition's buffer is bounded by the pattern's total maximum length: a held
start always has an extendable (not yet complete) candidate, which needs at
least one more row, so fewer than ``sum(max)`` rows can be unresolved.  Once a
match is emitted its raw rows are dropped; completed history does not linger.
Event-id uniqueness metadata lives in a separate set.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .events import Event, Pattern, RejectedInput

# Emission triggers ---------------------------------------------------------
# A result is "finalised" either after the N-th accepted data row of its
# partition, or when that partition's END arrives.  Global push ordering is
# deliberately not recorded, so interleaving cannot change per-partition output.


@dataclass
class PartitionMeta:
    last_seq: int | None = None
    seen_ids: set[str] = field(default_factory=set)
    closed: bool = False
    data_count: int = 0


@dataclass
class PatternPartitionState:
    pending: list[Event] = field(default_factory=list)
    peak: int = 0


def _analyze(rows: Sequence[Event], pattern: Pattern):
    """Return (best_alive_vector, is_complete) or (None, False).

    A vector is *alive* when the rows already seen are consistent with it:
    every position the vector maps onto an already-buffered row satisfies that
    variable's predicate.  Vectors longer than the buffer need no prediction:
    only their visible prefix is checked, so unseen input is never consulted.
    """
    t = len(rows)
    best_vec: tuple[int, ...] | None = None
    best_complete = False
    variables = pattern.variables
    mins = pattern.mins
    maxs = pattern.maxs
    for vec in pattern.allowed_counts():
        ok = True
        idx = 0
        for vi, cnt in enumerate(vec):
            if cnt == 0:  # not produced by allowed_counts, but cheap guard
                continue
            pred = pattern.definitions[variables[vi]]
            limit = min(idx + cnt, t)
            for pos in range(idx, limit):
                if not pred.matches(rows[pos]):
                    ok = False
                    break
            if not ok:
                break
            idx += cnt
        if not ok:
            continue
        if best_vec is None or vec > best_vec:
            best_vec = vec
            best_complete = sum(vec) <= t
    return best_vec, best_complete


def _best_complete(rows: Sequence[Event], pattern: Pattern):
    """Lexicographically greatest count vector fully contained in ``rows``."""
    t = len(rows)
    best_vec: tuple[int, ...] | None = None
    for vec in pattern.allowed_counts():
        if sum(vec) > t:
            continue
        ok = True
        idx = 0
        for vi, cnt in enumerate(vec):
            pred = pattern.definitions[pattern.variables[vi]]
            for pos in range(idx, idx + cnt):
                if not pred.matches(rows[pos]):
                    ok = False
                    break
            if not ok:
                break
            idx += cnt
        if ok and (best_vec is None or vec > best_vec):
            best_vec = vec
    return best_vec


class Matcher:
    """Consume interleaved events one by one; all patterns run in parallel."""

    def __init__(self, patterns: Iterable[Pattern]):
        self.patterns = list(patterns)
        if not self.patterns:
            raise ValueError("at least one pattern is required")
        ids = [p.id for p in self.patterns]
        if len(set(ids)) != len(ids):
            raise ValueError("pattern ids must be unique")
        self._meta: dict[str, PartitionMeta] = {}
        self._states: dict[tuple[str, str], PatternPartitionState] = {}
        self.results: list[dict] = []
        self.log: list[dict] = []
        self._push_no = 0

    # -- introspection ----------------------------------------------------
    def pending_len(self, pattern_id: str, partition: str) -> int:
        st = self._states.get((pattern_id, partition))
        return len(st.pending) if st else 0

    def pending_start(self, pattern_id: str, partition: str) -> int | None:
        st = self._states.get((pattern_id, partition))
        return st.pending[0].seq if st and st.pending else None

    def peak(self, pattern_id: str, partition: str) -> int:
        """Largest number of raw rows simultaneously buffered for this pair."""
        st = self._states.get((pattern_id, partition))
        return st.peak if st else 0

    def peaks(self) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {p.id: {} for p in self.patterns}
        for (pid, part), st in self._states.items():
            out.setdefault(pid, {})[part] = st.peak
        return out

    def partition_info(self) -> dict[str, dict]:
        return {
            part: {"data_rows": meta.data_count, "closed": meta.closed}
            for part, meta in self._meta.items()
        }

    def _state(self, pattern: Pattern, partition: str) -> PatternPartitionState:
        key = (pattern.id, partition)
        st = self._states.get(key)
        if st is None:
            st = PatternPartitionState()
            self._states[key] = st
        return st

    # -- emission ---------------------------------------------------------
    def _emit(
        self,
        pattern: Pattern,
        partition: str,
        rows: Sequence[Event],
        vec: tuple[int, ...],
        trigger: dict,
    ) -> dict:
        length = sum(vec)
        block = list(rows[:length])
        variables: dict[str, dict] = {}
        idx = 0
        total = 0
        for vi, cnt in enumerate(vec):
            var = pattern.variables[vi]
            part = block[idx : idx + cnt]
            var_total = sum(e.value for e in part)
            variables[var] = {
                "count": cnt,
                "event_ids": [e.event_id for e in part],
                "seqs": [e.seq for e in part],
                "value_sum": var_total,
                "events": [dict(e.raw) if e.raw else {"event_id": e.event_id, "seq": e.seq} for e in part],
            }
            total += var_total
            idx += cnt
        start, end = block[0], block[-1]
        vec_tag = "-".join(str(n) for n in vec)
        result = {
            "match_id": f"{pattern.id}:{partition}:{start.event_id}:{end.event_id}:{vec_tag}",
            "pattern": pattern.id,
            "partition": partition,
            "skip": pattern.skip,
            "start_event": {"event_id": start.event_id, "seq": start.seq},
            "end_event": {"event_id": end.event_id, "seq": end.seq},
            "length": length,
            "counts": {pattern.variables[i]: vec[i] for i in range(len(vec))},
            "count_vector": list(vec),
            "variables": variables,
            "value_sum": total,
            "finalised": trigger,
        }
        self.results.append(result)
        return result

    # -- settling ---------------------------------------------------------
    def _settle_data(self, pattern: Pattern, partition: str, data_index: int) -> list[dict]:
        st = self._state(pattern, partition)
        emitted: list[dict] = []
        while st.pending:
            best, complete = _analyze(st.pending, pattern)
            if best is None:
                # Row 0 cannot begin any allocation: move start forward.
                del st.pending[0]
                continue
            if not complete:
                # A lexicographically greater allocation may still appear.
                break
            emitted.append(
                self._emit(
                    pattern,
                    partition,
                    st.pending,
                    best,
                    {"kind": "data", "data_index": data_index},
                )
            )
            drop = 1 if pattern.skip == "next_row" else sum(best)
            del st.pending[:drop]
        return emitted

    def _settle_end(self, pattern: Pattern, partition: str) -> list[dict]:
        st = self._state(pattern, partition)
        emitted: list[dict] = []
        while st.pending:
            best = _best_complete(st.pending, pattern)
            if best is None:
                # No allocation fits in the remaining buffered rows.
                del st.pending[0]
                continue
            emitted.append(
                self._emit(
                    pattern,
                    partition,
                    st.pending,
                    best,
                    {"kind": "end"},
                )
            )
            drop = 1 if pattern.skip == "next_row" else sum(best)
            del st.pending[:drop]
        return emitted

    # -- public API -------------------------------------------------------
    def push(self, event: Event | Mapping) -> list[dict]:
        """Consume one data row; return results finalised by this push."""
        if isinstance(event, Mapping):
            event = Event.from_dict(dict(event))
        if not isinstance(event, Event):
            raise TypeError("push expects an Event or a mapping")
        partition = event.partition
        meta = self._meta.get(partition)
        if meta is None:
            meta = PartitionMeta()
            self._meta[partition] = meta
        if meta.closed:
            raise RejectedInput(f"partition {partition!r} is closed; data rejected")
        if meta.last_seq is not None and event.seq <= meta.last_seq:
            raise RejectedInput(
                f"partition {partition!r}: seq {event.seq} not strictly > {meta.last_seq}"
            )
        if event.event_id in meta.seen_ids:
            raise RejectedInput(
                f"partition {partition!r}: duplicate event_id {event.event_id!r}"
            )
        meta.last_seq = event.seq
        meta.seen_ids.add(event.event_id)
        meta.data_count += 1
        self._push_no += 1
        data_index = meta.data_count

        emitted: list[dict] = []
        details: dict[str, dict] = {}
        for pattern in self.patterns:
            st = self._state(pattern, partition)
            st.pending.append(event)
            if len(st.pending) > st.peak:
                st.peak = len(st.pending)
            got = self._settle_data(pattern, partition, data_index)
            emitted.extend(got)
            details[pattern.id] = {
                "pending_start_seq": st.pending[0].seq if st.pending else None,
                "pending_len": len(st.pending),
                "buffer_peak": st.peak,
                "emitted": [r["match_id"] for r in got],
            }
        self.log.append(
            {
                "push_no": self._push_no,
                "partition": partition,
                "kind": "data",
                "data_index": data_index,
                "seq": event.seq,
                "event_id": event.event_id,
                "patterns": details,
                "emitted": [r["match_id"] for r in emitted],
            }
        )
        return emitted

    def end(self, partition: str) -> list[dict]:
        """Close a partition and settle all still-representable matches."""
        meta = self._meta.get(partition)
        if meta is None:
            meta = PartitionMeta()
            self._meta[partition] = meta
        if meta.closed:
            raise RejectedInput(f"partition {partition!r} already closed")
        meta.closed = True
        self._push_no += 1

        emitted: list[dict] = []
        details: dict[str, dict] = {}
        for pattern in self.patterns:
            st = self._state(pattern, partition)
            got = self._settle_end(pattern, partition)
            emitted.extend(got)
            details[pattern.id] = {
                "pending_start_seq": st.pending[0].seq if st.pending else None,
                "pending_len": len(st.pending),
                "buffer_peak": st.peak,
                "emitted": [r["match_id"] for r in got],
            }
        self.log.append(
            {
                "push_no": self._push_no,
                "partition": partition,
                "kind": "end",
                "patterns": details,
                "emitted": [r["match_id"] for r in emitted],
            }
        )
        return emitted

    def close_all(self) -> list[dict]:
        """Convenience for a finite input: END every still-open partition."""
        emitted: list[dict] = []
        for partition, meta in list(self._meta.items()):
            if not meta.closed:
                emitted.extend(self.end(partition))
        return emitted
