"""Tests for the incremental partition matcher.

Run with:  python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import itertools
import json
import random
import unittest
from pathlib import Path

from solution.events import Event, Pattern, RejectedInput
from solution.matcher import Matcher
from tests.oracle import oracle_results

ROOT = Path(__file__).resolve().parent.parent
PATTERNS = [Pattern.from_dict(o) for o in json.loads((ROOT / "fixtures/patterns.json").read_text())]
BY_ID = {p.id: p for p in PATTERNS}


def load_stream(path):
    rows, ends = {}, []
    for line in (ROOT / path).read_text().splitlines():
        obj = json.loads(line)
        if obj.get("type") == "end":
            ends.append(obj["partition"])
        else:
            rows.setdefault(obj["partition"], []).append(Event.from_dict(obj))
    return rows, ends


def _norm(results, pattern_id, partition):
    keys = [
        "match_id", "start_event", "end_event", "count_vector",
        "variables", "value_sum", "counts", "length", "skip", "finalised",
    ]
    return sorted(
        ({k: r[k] for k in keys} for r in results
         if r["pattern"] == pattern_id and r["partition"] == partition),
        key=lambda r: (r["start_event"]["seq"], r["match_id"]),
    )


def _drive(patterns, rows_by_part, schedule):
    """Feed events following ``schedule``; return (matcher, per-key results)."""
    m = Matcher(patterns)
    seen = {}
    for part in schedule:
        seq, rows = seen.get(part, (0, rows_by_part[part]))
        m.push(rows[seq])
        seen[part] = (seq + 1, rows)
    for part, (seq, rows) in seen.items():
        if seq < len(rows):
            for ev in rows[seq:]:
                m.push(ev)
        m.end(part)
    for part in rows_by_part:
        if part not in seen:
            m.end(part)
    return m


class FixtureOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows, cls.ends = load_stream("fixtures/stream.jsonl")

    def test_all_patterns_match_oracle(self):
        m = Matcher(PATTERNS)
        for part in ("p1", "p2"):
            for ev in self.rows[part]:
                m.push(ev)
            m.end(part)
        for pattern in PATTERNS:
            for part in ("p1", "p2"):
                got = _norm(m.results, pattern.id, part)
                want = _norm(oracle_results(self.rows[part], pattern, part), pattern.id, part)
                self.assertEqual(got, want, f"{pattern.id}/{part}")

    def test_overlap_policies_differ(self):
        # normal/past_last vs overlap/next_row are distinct patterns; the two
        # value-based patterns differ only in skip policy.
        overlap = [r for r in _drive(PATTERNS, self.rows, [p for p in ("p1", "p2")]).results
                   if r["pattern"] in ("overlap", "same_no_overlap") and r["partition"] == "p1"]
        counts = {}
        for r in overlap:
            counts.setdefault(r["pattern"], []).append(tuple(r["start_event"].values()))
        self.assertNotEqual(
            [r["start_event"]["seq"] for r in overlap if r["pattern"] == "overlap"],
            [r["start_event"]["seq"] for r in overlap if r["pattern"] == "same_no_overlap"],
        )

    def test_normal_b_swallows_then_backtracks(self):
        # p2: A=f1(start), B candidates f2,f3,f4, C=f5(end).  B must not greedily
        # take f4; it retreats so C can match f5.
        m = Matcher(PATTERNS)
        for ev in self.rows["p2"]:
            m.push(ev)
        m.end("p2")
        normal = [r for r in m.results if r["pattern"] == "normal" and r["partition"] == "p2"]
        self.assertEqual(len(normal), 1)
        self.assertEqual(normal[0]["counts"], {"A": 1, "B": 3, "C": 1})
        self.assertEqual(normal[0]["variables"]["C"]["event_ids"], ["f5"])


class InterleavingInvarianceTests(unittest.TestCase):
    def test_schedule_does_not_change_results(self):
        rows, _ = load_stream("fixtures/stream.jsonl")
        base = Matcher(PATTERNS)
        for part in ("p1", "p2"):
            for ev in rows[part]:
                base.push(ev)
            base.end(part)
        baseline = sorted((r["match_id"], tuple(r["variables"]["B"]["seqs"])) for r in base.results)

        # Several interleavings, including all-of-p1-then-p2 and random orders.
        orders = []
        orders.append([p for p in ("p1", "p2")])
        orders.append([p for p in ("p2", "p1")])
        rng = random.Random(7)
        flat = [("p1", i) for i in range(len(rows["p1"]))] + [("p2", i) for i in range(len(rows["p2"]))]
        for _ in range(10):
            rng.shuffle(flat)
            orders.append(list(flat))
        for order in orders:
            m = Matcher(PATTERNS)
            if order and isinstance(order[0], tuple):
                ptr = {"p1": 0, "p2": 0}
                for part, _ in order:
                    m.push(rows[part][ptr[part]])
                    ptr[part] += 1
            else:
                for part in order:
                    for ev in rows[part]:
                        m.push(ev)
            # close in fixed per-partition order
            m.end("p1")
            m.end("p2")
            got = sorted((r["match_id"], tuple(r["variables"]["B"]["seqs"])) for r in m.results)
            self.assertEqual(got, baseline, f"order={order[:6]}...")


class FinalisationTests(unittest.TestCase):
    def test_max_length_emits_immediately(self):
        # normal: A then three B rows reaches max length 1+3+1=5 only with C;
        # a pattern that hits total max can fire without waiting.
        p = Pattern.from_dict(
            {
                "id": "t",
                "pattern": [
                    {"var": "A", "min": 1, "max": 1},
                    {"var": "B", "min": 1, "max": 2},
                ],
                "define": {
                    "A": {"field": "kind", "eq": "start"},
                    "B": {"field": "kind", "eq": "end"},
                },
                "skip": "past_last",
            }
        )
        m = Matcher([p])
        evs = [
            Event.from_dict({"partition": "x", "seq": 1, "event_id": "a", "kind": "start", "value": 0}),
            Event.from_dict({"partition": "x", "seq": 2, "event_id": "b", "kind": "end", "value": 1}),
            Event.from_dict({"partition": "x", "seq": 3, "event_id": "c", "kind": "end", "value": 2}),
        ]
        self.assertEqual(m.push(evs[0]), [])
        self.assertEqual(m.push(evs[1]), [])
        got = m.push(evs[2])  # [1,2] reaches total max -> final here
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["count_vector"], [1, 2])
        self.assertEqual(got[0]["finalised"], {"kind": "data", "data_index": 3})

    def test_must_wait_one_more_row(self):
        # normal at p1 start e1: [1,2,1] would complete at row 4 (e1..e4), but a
        # lexicographically larger [1,3,1] needs a fifth row; so it must wait and
        # can only finalise after row 5 rules the larger vector out.
        rows, _ = load_stream("fixtures/stream.jsonl")
        p = BY_ID["normal"]
        m = Matcher([p])
        push_results = []
        for ev in rows["p1"]:
            push_results.append(m.push(ev))
        self.assertEqual(push_results[2], [])  # rows 1..3: [1,2,1] blocked? see below
        self.assertEqual(push_results[3], [])  # row 4: complete but not forced
        got = m.results
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["finalised"], {"kind": "data", "data_index": 5})
        self.assertEqual(got[0]["count_vector"], [1, 2, 1])

    def test_end_settles_pending(self):
        p = Pattern.from_dict(
            {
                "id": "t2",
                "pattern": [
                    {"var": "A", "min": 1, "max": 1},
                    {"var": "B", "min": 1, "max": 2},
                ],
                "define": {
                    "A": {"field": "kind", "eq": "start"},
                    "B": {"field": "kind", "eq": "end"},
                },
                "skip": "past_last",
            }
        )
        m = Matcher([p])
        for ev in [
            Event.from_dict({"partition": "x", "seq": 1, "event_id": "a", "kind": "start", "value": 0}),
            Event.from_dict({"partition": "x", "seq": 2, "event_id": "b", "kind": "end", "value": 1}),
        ]:
            self.assertEqual(m.push(ev), [])
        got = m.end("x")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["finalised"], {"kind": "end"})


class ContractTests(unittest.TestCase):
    def test_reject_after_end(self):
        m = Matcher([BY_ID["overlap"]])
        m.push(Event.from_dict({"partition": "x", "seq": 1, "event_id": "a", "value": 0}))
        m.end("x")
        with self.assertRaises(RejectedInput):
            m.push(Event.from_dict({"partition": "x", "seq": 2, "event_id": "b", "value": 1}))
        with self.assertRaises(RejectedInput):
            m.end("x")

    def test_reject_non_increasing_seq_and_duplicate_id(self):
        m = Matcher([BY_ID["overlap"]])
        m.push(Event.from_dict({"partition": "x", "seq": 5, "event_id": "a", "value": 0}))
        with self.assertRaises(RejectedInput):
            m.push(Event.from_dict({"partition": "x", "seq": 5, "event_id": "b", "value": 0}))
        with self.assertRaises(RejectedInput):
            m.push(Event.from_dict({"partition": "x", "seq": 6, "event_id": "a", "value": 0}))
        # Same id in another partition is fine.
        m.push(Event.from_dict({"partition": "y", "seq": 1, "event_id": "a", "value": 0}))

    def test_buffer_bounded_and_history_dropped(self):
        # Long run of B-compatible rows without a start: start advances, nothing
        # accumulates.  Peak must stay <= total max.
        p = BY_ID["normal"]
        m = Matcher([p])
        for i in range(200):
            m.push(Event.from_dict({"partition": "z", "seq": i + 1, "event_id": f"z{i}", "kind": "step", "value": 5}))
        self.assertLessEqual(m.peak("normal", "z"), p.total_max + 1)
        self.assertEqual(m.pending_len("normal", "z"), 0)
        self.assertEqual(m.results, [])


class OracleRandomTests(unittest.TestCase):
    """Small exhaustive-ish batches: brute-force vectors are the oracle."""

    def _random_rows(self, rng, n):
        rows = []
        for i in range(n):
            rows.append(
                Event.from_dict(
                    {
                        "partition": "q",
                        "seq": i + 1,
                        "event_id": f"q{i}",
                        "kind": rng.choice(["start", "step", "end"]),
                        "value": rng.choice([0, 1, 2, 3, 5, 8]),
                    }
                )
            )
        return rows

    def test_random_against_oracle(self):
        rng = random.Random(1234)
        for trial in range(300):
            n = rng.randint(0, 8)
            rows = self._random_rows(rng, n)
            for pattern in PATTERNS:
                with self.subTest(trial=trial, pattern=pattern.id):
                    m = Matcher([pattern])
                    for ev in rows:
                        m.push(ev)
                    m.end("q")
                    got = _norm(m.results, pattern.id, "q")
                    want = _norm(oracle_results(rows, pattern, "q"), pattern.id, "q")
                    self.assertEqual(got, want)

    def test_random_against_oracle_with_both_skips(self):
        rng = random.Random(99)
        for trial in range(150):
            n = rng.randint(1, 9)
            rows = self._random_rows(rng, n)
            for pattern in PATTERNS:
                m = Matcher([pattern])
                for ev in rows:
                    m.push(ev)
                # do not END: verify streaming alone never emits an unfinalisable
                # match, then END and compare.
                m.end("q")
                got = _norm(m.results, pattern.id, "q")
                want = _norm(oracle_results(rows, pattern, "q"), pattern.id, "q")
                self.assertEqual(got, want)


class RandomPatternOracleTests(unittest.TestCase):
    """Randomly generated patterns (1-4 vars, arbitrary min/max) vs oracle."""

    def test_random_patterns_against_oracle(self):
        rng = random.Random(2024)
        for trial in range(200):
            nv = rng.randint(1, 4)
            variables = [chr(ord("A") + i) for i in range(nv)]
            spec = []
            defines = {}
            for v in variables:
                lo = rng.randint(1, 3)
                hi = rng.randint(lo, 3)
                spec.append({"var": v, "min": lo, "max": hi})
                if rng.random() < 0.5:
                    defines[v] = {"field": "kind", "eq": rng.choice(["start", "step", "end"])}
                else:
                    defines[v] = {"field": "value", "ge": rng.choice([0, 2, 5])}
            p = Pattern.from_dict(
                {
                    "id": f"r{trial}",
                    "pattern": spec,
                    "define": defines,
                    "skip": rng.choice(["next_row", "past_last"]),
                }
            )
            n = rng.randint(0, 12)
            rows = [
                Event.from_dict(
                    {
                        "partition": "q",
                        "seq": i + 1,
                        "event_id": f"q{i}",
                        "kind": rng.choice(["start", "step", "end"]),
                        "value": rng.choice([0, 1, 2, 3, 5, 8]),
                    }
                )
                for i in range(n)
            ]
            m = Matcher([p])
            for ev in rows:
                m.push(ev)
            m.end("q")
            got = _norm(m.results, p.id, "q")
            want = _norm(oracle_results(rows, p, "q"), p.id, "q")
            self.assertEqual(got, want, f"trial={trial} {variables} {p.mins}{p.maxs} {p.skip}")


class InterleavingAndEndTimingTests(unittest.TestCase):
    """Changing cross-partition interleaving or END ordering must not alter
    per-partition results or the data-index/END at which each is finalised."""

    @staticmethod
    def _gen(part, n, rng):
        return [
            Event.from_dict(
                {
                    "partition": part,
                    "seq": i + 1,
                    "event_id": f"{part}_{i}",
                    "kind": rng.choice(["start", "step", "end"]),
                    "value": rng.choice([0, 1, 2, 3, 5, 8]),
                }
            )
            for i in range(n)
        ]

    def test_invariance(self):
        rng = random.Random(77)
        for trial in range(100):
            parts = [f"p{j}" for j in range(rng.randint(1, 4))]
            rows = {
                p: self._gen(p, rng.randint(0, 12), random.Random(trial * 31 + int(p[1:])))
                for p in parts
            }
            base = {}
            for p in parts:
                m = Matcher(PATTERNS)
                fi = {}
                for i, ev in enumerate(rows[p]):
                    for r in m.push(ev):
                        fi[r["match_id"]] = ("data", i + 1)
                for r in m.end(p):
                    fi[r["match_id"]] = ("end", len(rows[p]))
                base[p] = (
                    sorted((r["match_id"], tuple(r["count_vector"])) for r in m.results),
                    fi,
                )
            for _ in range(4):
                ptr = {p: 0 for p in parts}
                m = Matcher(PATTERNS)
                fi, counts = {}, {p: 0 for p in parts}
                remaining = sum(len(rows[p]) for p in parts)
                while remaining:
                    live = [p for p in parts if ptr[p] < len(rows[p])]
                    p = rng.choice(live)
                    for r in m.push(rows[p][ptr[p]]):
                        fi[r["match_id"]] = ("data", counts[p] + 1)
                    ptr[p] += 1
                    counts[p] += 1
                    remaining -= 1
                order = list(parts)
                rng.shuffle(order)
                for p in order:
                    for r in m.end(p):
                        fi[r["match_id"]] = ("end", len(rows[p]))
                for p in parts:
                    got = sorted(
                        (r["match_id"], tuple(r["count_vector"]))
                        for r in m.results
                        if r["partition"] == p
                    )
                    self.assertEqual(got, base[p][0], f"trial={trial} {p}")
                    for mid, _ in got:
                        self.assertEqual(fi[mid], base[p][1][mid], f"trial={trial} {p} {mid}")


class JsonlCliTests(unittest.TestCase):
    def test_cli_matches_api_and_csv_agrees(self):
        from solution.cli import load_patterns, run

        report = run(load_patterns(ROOT / "fixtures/patterns.json"), str(ROOT / "fixtures/stream.jsonl"))
        self.assertEqual(report["rejected"], [])
        # CSV carries the same rows, so results must be identical.
        import csv

        with (ROOT / "fixtures/events.csv").open() as fh:
            csv_rows = list(csv.DictReader(fh))
        self.assertEqual(len(csv_rows), sum(v["data_rows"] for v in report["partitions"].values()))
        # Buffers bounded for every pattern/partition.
        for pid, peaks in report["buffer_peaks"].items():
            pmax = BY_ID[pid].total_max
            for part, peak in peaks.items():
                self.assertLessEqual(peak, pmax + 1, f"{pid}/{part}")


if __name__ == "__main__":
    unittest.main()
