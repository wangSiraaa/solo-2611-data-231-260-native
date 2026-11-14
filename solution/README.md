# Tests / solution notes

## Layout

```
solution/
  events.py    Event, Pattern, predicate + validation primitives
  matcher.py   Matcher.push(event) / Matcher.end(partition): the incremental state machine
  cli.py       `python3 -m solution.cli` — consumes stream.jsonl line by line
tests/
  oracle.py    brute-force whole-partition oracle (enumerates count vectors)
  test_matcher.py
```

## Run

```bash
python3 -m solution.cli --results-only        # results only
python3 -m solution.cli                      # full report (results, push log, peaks)
python3 -m solution.cli --emit-each          # per-push output, live, to stdout
cat fixtures/stream.jsonl | python3 -m solution.cli --stream - --results-only
python3 -m unittest discover -s tests -v
```

## Semantics captured

* A match is a **contiguous** block of one partition's rows; every row maps to
  exactly one variable, in pattern order, repeated `min..max` times. No inner
  rows are skipped.
* At a given start several allocations may be complete; the **lexicographically
  greatest count vector** (compared in variable order) wins. This is why
  `normal` on `p2` takes `[1,3,1]` (B = f2,f3,f4, C = f5) rather than stopping
  B early, and why B is forced to *back off* from f4 when the later `C` row is
  absent.
* A match is emitted only when the rows seen so far make **every greater
  allocation impossible**. If a complete candidate exists but a larger one is
  still alive, it waits. If the buffer holds an extendable candidate, it holds
  state. Otherwise the start advances and cached rows are reprocessed.
* `end(partition)` immediately settles every match still representable by the
  buffered rows.
* `skip=next_row` resumes at `start+1`; `skip=past_last` resumes after the
  matched block — producing different overlaps on the same data
  (`overlap` vs `same_no_overlap`).
* Buffering per partition is bounded by `sum(max)`; each unresolved start needs
  one more row, so at most `sum(max)-1` rows are pending (checked ≤ `sum(max)+1`
  in tests). Emitted rows are deleted; completed captures are not retained.
  Event-id uniqueness lives in a `set`, not in the raw-row buffer.
* Interleaving is irrelevant: all state is keyed by `(pattern, partition)` and
  every output is stamped with a per-partition data index or `end`, never a
  global push number.

## Oracle

`tests/oracle.py` brute-forces every allowed count vector over the full
partition and independently computes both the greedy match set and the earliest
row index/END at which each match becomes forced. The matcher must agree with it
on the fixtures and on 450 random small batches, including the per-partition
finalisation point.
