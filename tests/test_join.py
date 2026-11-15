"""Tests for the streaming event-time LEFT JOIN.

Run with either ``python -m pytest tests`` or ``python -m unittest discover``.
"""

from __future__ import annotations

import json
import math
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evjoin import JoinError, Joiner, sort_facts  # noqa: E402
import reference  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
LOWER = CONFIG["lower_ms"]
UPPER = CONFIG["upper_ms"]


def load_events(name: str) -> list[dict]:
    events = []
    for line_no, raw in enumerate((ROOT / "fixtures" / name).read_text(encoding="utf-8").splitlines(), 1):
        if raw.strip():
            events.append(json.loads(raw))
    return events


def make(lower: int = LOWER, upper: int = UPPER) -> Joiner:
    return Joiner(lower, upper)


def D(side, id_, key, ts, value=None):  # noqa: N802 - compact test helper
    return {"op": "data", "side": side, "id": id_, "key": key, "ts": ts, "value": value}


def W(side, ts):  # noqa: N802
    return {"op": "watermark", "side": side, "ts": ts}


def E(side):  # noqa: N802
    return {"op": "end", "side": side}


def all_facts(joiner: Joiner) -> list[dict]:
    return joiner.facts


class FixtureTest(unittest.TestCase):
    def test_final_facts_match_reference(self):
        events = load_events("events.jsonl")
        joiner = make()
        for i, event in enumerate(events, 1):
            joiner.push(event, line_no=i)
        joiner.finish()

        got = Counter(
            (f["left_id"], f["right_id"]) for f in joiner.facts
        )
        expected = reference.reference(events, LOWER, UPPER)["facts"]
        self.assertEqual(got, expected)
        self.assertEqual(got["l3", None], 1)
        self.assertEqual(got["l5", None], 1)
        self.assertNotIn("late-r", [rid for _, rid in got if rid is not None])

    def test_late_keeps_line_and_watermark(self):
        events = load_events("events.jsonl")
        joiner = make()
        for i, event in enumerate(events, 1):
            joiner.push(event, line_no=i)
        self.assertEqual(len(joiner.late), 1)
        late = joiner.late[0]
        self.assertEqual(late["line"], 7)
        self.assertEqual(late["id"], "late-r")
        self.assertEqual(late["watermark"], 12)

    def test_exact_expected_matches(self):
        events = load_events("events.jsonl")
        joiner = make()
        for i, event in enumerate(events, 1):
            joiner.push(event, line_no=i)
        pairs = {(f["left_id"], f["right_id"]) for f in joiner.matches}
        self.assertEqual(
            pairs,
            {
                ("l1", "r1"), ("l1", "r2"),
                ("l2", "r1"), ("l2", "r2"), ("l2", "edge-r"),
                ("l4", "r1"), ("l4", "edge-r"),
            },
        )


class TimingTest(unittest.TestCase):
    def test_new_match_emitted_immediately(self):
        j = make()
        self.assertEqual(j.push(D("L", "l1", "a", 10)), [])
        out = j.push(D("R", "r1", "a", 11))
        self.assertEqual([(f["left_id"], f["right_id"]) for f in out], [("l1", "r1")])
        self.assertEqual(j.retained_ids(), {"L": ["l1"], "R": ["r1"]})

    def test_left_retired_at_interval_close(self):
        j = make()
        j.push(D("L", "l1", "a", 10))          # interval [8, 14)
        self.assertEqual(j.push(W("R", 13)), [])  # 13 < 14, still retirable later
        self.assertEqual(j.retained_ids()["L"], ["l1"])
        out = j.push(W("R", 14))                # 14 >= 14 -> safe
        self.assertEqual([(f["left_id"], f["right_id"]) for f in out], [("l1", None)])
        self.assertEqual(j.retained_ids()["L"], [])

    def test_matched_left_emits_no_null_fact_on_retirement(self):
        j = make()
        j.push(D("L", "l1", "a", 10))
        j.push(D("R", "r1", "a", 9))
        out = j.push(W("R", 14))
        self.assertEqual(out, [])
        self.assertEqual(j.retained_ids()["L"], [])

    def test_right_retirement_uses_left_watermark_strictly(self):
        j = make()
        j.push(D("R", "r1", "a", 10))  # matches lefts ts in (6, 12]
        self.assertEqual(j.push(W("L", 12)), [])  # ts == 12 still matchable
        self.assertEqual(j.retained_ids()["R"], ["r1"])
        j.push(W("L", 13))  # 13 > 12 -> safe
        self.assertEqual(j.retained_ids()["R"], [])

    def test_right_retirement_does_not_use_right_watermark(self):
        # A far-future right watermark must not evict a right record; only the
        # left watermark can make a right unmatchable by future lefts.
        j = make()
        j.push(D("R", "r1", "a", 10))
        j.push(W("R", 100))
        self.assertEqual(j.retained_ids()["R"], ["r1"])

    def test_trace_records_input_watermark_retained_and_output(self):
        j = make()
        j.push(D("L", "l1", "a", 10), line_no=1)
        j.push(W("R", 14), line_no=2)
        trace = j.trace
        self.assertEqual(trace[1]["step"], 2)
        self.assertEqual(trace[1]["line"], 2)
        self.assertEqual(trace[1]["watermarks"]["R"], 14)
        self.assertEqual(trace[1]["retained"], {"L": [], "R": []})
        self.assertEqual(
            trace[1]["outputs"],
            [{"left_id": "l1", "right_id": None, "left_ts": 10, "right_ts": None,
              "left_value": None, "right_value": None}],
        )


class SemanticsTest(unittest.TestCase):
    def test_many_to_many_each_pair_once(self):
        j = make()
        j.push(D("L", "l1", "a", 10))
        j.push(D("L", "l2", "a", 11))
        j.push(D("R", "r1", "a", 10))
        j.push(D("R", "r2", "a", 12))
        got = Counter((f["left_id"], f["right_id"]) for f in j.matches)
        self.assertEqual(
            got,
            Counter({
                ("l1", "r1"), ("l1", "r2"), ("l2", "r1"), ("l2", "r2"),
            }),
        )

    def test_right_endpoint_exclusive(self):
        j = make()
        j.push(D("L", "l1", "a", 10))       # [8, 14)
        self.assertEqual(j.push(D("R", "r", "a", 14)), [])
        self.assertEqual(j.push(D("R", "r2", "a", 13)), [
            {"left_id": "l1", "right_id": "r2", "left_ts": 10, "right_ts": 13,
             "left_value": None, "right_value": None}
        ])

    def test_left_endpoint_inclusive(self):
        j = make()
        j.push(D("L", "l1", "a", 10))
        out = j.push(D("R", "r", "a", 8))   # l.ts + lower == 8 -> match
        self.assertEqual([(f["left_id"], f["right_id"]) for f in out], [("l1", "r")])

    def test_ts_equal_to_watermark_is_still_legal(self):
        j = make()
        j.push(W("L", 12))
        out = j.push(D("L", "l1", "a", 12))  # ts == watermark -> legal, not late
        self.assertEqual(out, [])
        self.assertEqual(j.late, [])
        self.assertEqual(j.retained_ids()["L"], ["l1"])

    def test_ts_below_watermark_is_late_and_has_no_effect(self):
        j = make()
        j.push(D("R", "r1", "a", 10))
        j.push(W("L", 11))
        out = j.push(D("L", "old", "a", 10))  # 10 < 11 -> late
        self.assertEqual(out, [])
        self.assertEqual(j.late[0]["id"], "old")
        # Never buffered, so it cannot join with the buffered right.
        self.assertEqual(j.matches, [])

    def test_left_arriving_after_right(self):
        j = make()
        j.push(D("R", "r1", "a", 12))
        out = j.push(D("L", "l1", "a", 10))
        self.assertEqual([(f["left_id"], f["right_id"]) for f in out], [("l1", "r1")])

    def test_left_arriving_after_both_watermarks_past_interval_is_final(self):
        j = make()
        j.push(W("L", 10))
        j.push(W("R", 20))
        out = j.push(D("L", "l1", "a", 10))  # legal (== W_L), interval closed
        self.assertEqual([(f["left_id"], f["right_id"]) for f in out], [("l1", None)])
        self.assertEqual(j.retained_ids()["L"], [])

    def test_left_arriving_after_closed_interval_still_matches_buffered_right(self):
        j = make()
        j.push(W("L", 10))
        j.push(D("R", "r1", "a", 12))  # retained: W_L=10 <= r.ts - lower
        j.push(W("R", 20))             # closes l.ts=10 interval [8,14)
        out = j.push(D("L", "l1", "a", 10))
        self.assertEqual([(f["left_id"], f["right_id"]) for f in out], [("l1", "r1")])
        self.assertNotIn(None, [f["right_id"] for f in out])
        self.assertEqual(j.retained_ids()["L"], [])

    def test_right_matching_retired_left_is_impossible_by_construction(self):
        # A left retired at W_R == l.ts+upper can never see a legal right that
        # matches it: any future right has ts >= W_R >= l.ts+upper (exclusive).
        j = make()
        j.push(D("L", "l1", "a", 10))
        j.push(W("R", 14))
        j.push(D("R", "r", "a", 14))  # exactly at +inf bound -> no match
        self.assertEqual(j.matches, [])

    def test_empty_side_end_still_yields_unmatched_lefts(self):
        j = make()
        j.push(D("L", "l1", "a", 10))
        out = j.push(E("R"))
        self.assertEqual([(f["left_id"], f["right_id"]) for f in out], [("l1", None)])
        self.assertEqual(j.push(E("L")), [])

    def test_empty_left_side_flushes_rights_without_facts(self):
        j = make()
        j.push(D("R", "r1", "a", 10))
        j.push(E("L"))
        self.assertEqual(j.retained_ids()["R"], [])
        self.assertEqual(j.facts, [])
        j.push(E("R"))

    def test_key_isolation(self):
        j = make()
        j.push(D("L", "l1", "a", 10))
        j.push(D("R", "r1", "b", 10))
        self.assertEqual(j.matches, [])


class LifecycleTest(unittest.TestCase):
    def test_finish_requires_both_sides_ended(self):
        j = make()
        j.push(E("R"))
        with self.assertRaises(JoinError):
            j.finish()

    def test_eof_is_not_end(self):
        j = make()
        j.push(D("L", "l1", "a", 10))
        with self.assertRaises(JoinError):
            j.finish()
        self.assertEqual(j.retained_ids()["L"], ["l1"])

    def test_finish_after_both_ends_ok(self):
        j = make()
        j.push(E("L"))
        j.push(E("R"))
        self.assertEqual(j.finish(), [])
        j.finish()  # idempotent

    def test_push_after_finish_fails(self):
        j = make()
        j.push(E("L"))
        j.push(E("R"))
        j.finish()
        with self.assertRaises(JoinError):
            j.push(D("L", "l", "a", 1))


class InvalidInputTest(unittest.TestCase):
    def test_duplicate_id_fails(self):
        j = make()
        j.push(D("L", "x", "a", 10))
        with self.assertRaises(JoinError):
            j.push(D("R", "x", "a", 10))

    def test_duplicate_id_across_sides_fails(self):
        j = make()
        j.push(D("L", "x", "a", 10))
        with self.assertRaises(JoinError):
            j.push(D("L", "x", "a", 11))

    def test_watermark_regression_fails(self):
        j = make()
        j.push(W("L", 5))
        with self.assertRaises(JoinError):
            j.push(W("L", 4))

    def test_flat_watermark_allowed(self):
        j = make()
        j.push(W("L", 5))
        j.push(W("L", 5))
        self.assertEqual(j.watermark["L"], 5)

    def test_data_after_end_fails(self):
        j = make()
        j.push(E("L"))
        with self.assertRaises(JoinError):
            j.push(D("L", "l", "a", 10))

    def test_watermark_after_end_fails(self):
        j = make()
        j.push(E("L"))
        with self.assertRaises(JoinError):
            j.push(W("L", 10))

    def test_double_end_fails(self):
        j = make()
        j.push(E("L"))
        with self.assertRaises(JoinError):
            j.push(E("L"))

    def test_bad_records_fail(self):
        bad = [
            {"op": "nope", "side": "L"},
            {"op": "data", "side": "X", "id": "a", "key": "k", "ts": 1, "value": 0},
            {"op": "data", "side": "L", "id": "a", "key": "k", "ts": 1.5, "value": 0},
            {"op": "data", "side": "L", "id": "a", "key": "k", "ts": True, "value": 0},
            {"op": "data", "side": "L", "id": "a", "ts": 1, "value": 0},
            {"op": "data", "side": "L", "id": "a", "key": "k", "value": 0},
            {"op": "watermark", "side": "L", "ts": "2"},
        ]
        for event in bad:
            with self.subTest(event=event):
                with self.assertRaises(JoinError):
                    make().push(event)

    def test_invalid_watermark_fixture_fails(self):
        events = load_events("invalid-watermark.jsonl")
        j = make()
        with self.assertRaises(JoinError):
            for i, event in enumerate(events, 1):
                j.push(event, line_no=i)


class ExportTest(unittest.TestCase):
    def test_stable_sort_by_ids(self):
        facts = [
            {"left_id": "l2", "right_id": "r1"},
            {"left_id": "l1", "right_id": "r2"},
            {"left_id": "l1", "right_id": None},
            {"left_id": "l1", "right_id": "r1"},
        ]
        ordered = [(f["left_id"], f["right_id"]) for f in sort_facts(facts)]
        self.assertEqual(
            ordered,
            [("l1", None), ("l1", "r1"), ("l1", "r2"), ("l2", "r1")],
        )

    def test_cli_writes_outputs(self):
        import tempfile
        from evjoin.cli import run

        with tempfile.TemporaryDirectory() as tmp:
            summary = run(str(ROOT / "config.json"), str(ROOT / "fixtures" / "events.jsonl"), tmp)
            self.assertEqual(summary["late"], 1)
            matches = [json.loads(x) for x in (Path(tmp) / "matches.jsonl").read_text().splitlines()]
            late = [json.loads(x) for x in (Path(tmp) / "late.jsonl").read_text().splitlines()]
            trace = [json.loads(x) for x in (Path(tmp) / "trace.jsonl").read_text().splitlines()]
            self.assertEqual(len(matches), summary["facts"])
            self.assertEqual(len(late), 1)
            self.assertEqual(len(trace), 15)
            self.assertTrue(all("retained" in row and "outputs" in row for row in trace))


class ReferenceRandomizedTest(unittest.TestCase):
    """Cross-check against the independent oracle on random legal streams."""

    def _stream(self, seed: int) -> list[dict]:
        import random

        rng = random.Random(seed)
        events: list[dict] = []
        wm = {"L": -50, "R": -50}
        prev = {"L": -50, "R": -50}
        ended = {"L": False, "R": False}
        ids = 0
        for _ in range(rng.randint(20, 80)):
            side = rng.choice("LR")
            choice = rng.random()
            if choice < 0.15:
                if not ended[side]:
                    # non-decreasing watermark, sometimes flat
                    wm[side] = max(wm[side], wm[side] + rng.randint(-5, 20))
                    events.append(W(side, wm[side]))
            elif choice < 0.2:
                if not ended[side] and rng.random() < 0.3:
                    ended[side] = True
                    events.append(E(side))
            else:
                if ended[side]:
                    continue
                ids += 1
                ts = wm[side] + rng.randint(-8, 25)  # some late, some legal
                events.append(D(side, f"id{ids}", rng.choice("abc"), ts, ids))
        # end any unfinished sides
        for side in "LR":
            if not ended[side]:
                events.append(E(side))
        return events

    def test_random_streams(self):
        for seed in range(60):
            with self.subTest(seed=seed):
                events = self._stream(seed)
                j = make()
                for i, event in enumerate(events, 1):
                    j.push(event, line_no=i)
                j.finish()
                got = Counter((f["left_id"], f["right_id"]) for f in j.facts)
                expected = reference.reference(events, LOWER, UPPER)
                self.assertEqual(got, expected["facts"])
                self.assertEqual(Counter(x["id"] for x in j.late), expected["late"])

    def test_random_streams_early_finish_raises(self):
        events = self._stream(1)
        # Drop the final end event to simulate EOF without completion.
        events = [e for e in events if e["op"] != "end"]
        if any(e["op"] != "end" for e in events):
            j = make()
            for e in events:
                j.push(e)
            with self.assertRaises(JoinError):
                j.finish()


class ConfigTest(unittest.TestCase):
    def test_from_config_dict(self):
        j = Joiner.from_config_dict({"lower_ms": 0, "upper_ms": 1})
        self.assertEqual((j.lower_ms, j.upper_ms), (0, 1))

    def test_bad_config(self):
        with self.assertRaises(JoinError):
            Joiner.from_config_dict({"lower_ms": 0})
        with self.assertRaises(ValueError):
            Joiner("0", 1)


if __name__ == "__main__":
    unittest.main()
