"""Independent brute-force oracle used to validate the incremental matcher.

The oracle has the whole partition up front (test-only) and enumerates every
allowed repetition-count vector by brute force.  It deliberately shares no code
with ``solution.matcher`` beyond the pattern/event primitives, so agreement is
meaningful.
"""

from __future__ import annotations

from solution.events import Event, Pattern


def _check_span(rows, start, vec, pattern):
    idx = start
    for vi, cnt in enumerate(vec):
        pred = pattern.definitions[pattern.variables[vi]]
        for k in range(idx, idx + cnt):
            if not pred.matches(rows[k]):
                return False
        idx += cnt
    return True


def oracle_matches(rows, pattern):
    """Final greedy matches: list of (start_index, count_vector)."""
    n = len(rows)
    out = []
    pos = 0
    while pos < n:
        best = None
        for vec in pattern.allowed_counts():
            if pos + sum(vec) > n:
                continue
            if _check_span(rows, pos, vec, pattern) and (best is None or vec > best):
                best = vec
        if best is None:
            pos += 1
            continue
        out.append((pos, best))
        pos = pos + 1 if pattern.skip == "next_row" else pos + sum(best)
    return out


def _alive(rows, start, t, vec, pattern):
    """Is ``vec`` consistent with only the first ``t`` rows from ``start``?"""
    idx = start
    for vi, cnt in enumerate(vec):
        pred = pattern.definitions[pattern.variables[vi]]
        for k in range(idx, idx + cnt):
            if (k - start) >= t:
                return True  # rest lies beyond the observed prefix
            if not pred.matches(rows[k]):
                return False
        idx += cnt
    return True


def earliest_finalisation(rows, pattern, start, chosen):
    """Earliest point at which ``chosen`` is forced.

    Returns (kind, data_index) where kind is "data" (index of the data row,
    1-based within the partition) or ("end", None) if only partition end
    settles it.
    """
    bigger = [w for w in pattern.allowed_counts() if w > chosen]
    length = sum(chosen)
    for t in range(length, len(rows) - start + 1):
        if all(not _alive(rows, start, t, w, pattern) for w in bigger):
            # ``t`` counts rows observed from ``start``; report the global
            # 1-based data index, matching the matcher's trigger.
            return ("data", start + t)
    return ("end", None)


def oracle_results(rows, pattern, partition):
    """Normalised oracle results comparable to Matcher output."""
    results = []
    for start, vec in oracle_matches(rows, pattern):
        block = rows[start : start + sum(vec)]
        variables = {}
        idx = 0
        total = 0
        for vi, cnt in enumerate(vec):
            var = pattern.variables[vi]
            part = block[idx : idx + cnt]
            vsum = sum(e.value for e in part)
            total += vsum
            variables[var] = {
                "count": cnt,
                "event_ids": [e.event_id for e in part],
                "seqs": [e.seq for e in part],
                "value_sum": vsum,
                "events": [dict(e.raw) if e.raw else {"event_id": e.event_id, "seq": e.seq} for e in part],
            }
            idx += cnt
        kind, data_index = earliest_finalisation(rows, pattern, start, vec)
        finalised = {"kind": kind} if kind == "end" else {"kind": "data", "data_index": data_index}
        results.append(
            {
                "pattern": pattern.id,
                "partition": partition,
                "skip": pattern.skip,
                "start_event": {"event_id": block[0].event_id, "seq": block[0].seq},
                "end_event": {"event_id": block[-1].event_id, "seq": block[-1].seq},
                "length": sum(vec),
                "counts": {pattern.variables[i]: vec[i] for i in range(len(vec))},
                "count_vector": list(vec),
                "variables": variables,
                "value_sum": total,
                "finalised": finalised,
                "match_id": f"{pattern.id}:{partition}:{block[0].event_id}:{block[-1].event_id}:"
                + "-".join(str(n) for n in vec),
            }
        )
    return results
