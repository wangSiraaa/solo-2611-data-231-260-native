"""单元测试：python3 -m unittest tests -v"""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path

import rollup

ROOT = Path(__file__).parent


def fresh_conn():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    Path(tmp.name).unlink()
    subprocess.run([sys.executable, str(ROOT / "prepare.py"), tmp.name],
                   check=True)
    return sqlite3.connect(tmp.name)


class RollupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn = fresh_conn()
        cls.model = rollup.load_model(ROOT / "fixtures/model.json")
        cls.queries = {q["id"]: q for q in json.loads(
            (ROOT / "fixtures/queries.json").read_text(encoding="utf-8"))}

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def verdicts(self, qid):
        p = rollup.plan(self.model, self.queries[qid], self.conn)
        return p, {v.name: v for v in p.verdicts}

    def test_avg_only_rejected(self):
        # 只有 avg_amount 没有权重的候选不够，不能按组数平均
        for qid in self.queries:
            _, vs = self.verdicts(qid)
            self.assertFalse(vs["v_avg"].usable)
            self.assertIn("权重", vs["v_avg"].reason)

    def test_stale_snapshot_rejected(self):
        for qid in self.queries:
            _, vs = self.verdicts(qid)
            self.assertFalse(vs["v_stale"].usable)
            self.assertIn("snapshot", vs["v_stale"].reason)

    def test_wrong_domain_rejected(self):
        # 只含 A 商品的表不能回答全商品或 B 商品
        _, vs = self.verdicts("region_avg")
        self.assertFalse(vs["v_a"].usable)
        _, vs = self.verdicts("b_only")
        self.assertFalse(vs["v_a"].usable)
        self.assertIn("不覆盖", vs["v_a"].reason)

    def test_predicate_fully_covers_condition(self):
        # product=A 已由 v_a 谓词完全满足 → 无需该列、无残余过滤
        p, _ = self.verdicts("a_only")
        self.assertEqual(p.chosen, "v_a")
        self.assertEqual(p.residual_where, {})

    def test_selection_min_rows_then_name(self):
        # v_daily(4 行) < v_detail(6 行)，选 v_daily
        p, vs = self.verdicts("region_avg")
        self.assertEqual(p.chosen, "v_daily")
        self.assertEqual(vs["v_daily"].row_count, 4)
        self.assertEqual(vs["v_detail"].row_count, 6)

    def test_weighted_avg(self):
        q = self.queries["region_avg"]
        p = rollup.plan(self.model, q, self.conn)
        entries, tables = rollup.run(self.conn, self.model, q, p)
        n = next(e for e in entries if e["region"] == "N")
        # 170/5=34，按组数平均的错误口径为 (110/3+60/2)/2≈33.33
        self.assertEqual(n["amount_avg"], Fraction(170, 5))
        self.assertNotEqual(n["amount_avg"], (Fraction(110, 3) + 30) / 2)
        self.assertEqual(tables, {"v_daily"})  # 实际未读 fact

    def test_null_group_and_null_sum(self):
        q = self.queries["region_avg"]
        entries, _ = rollup.run(self.conn, self.model, q,
                                rollup.plan(self.model, q, self.conn))
        null_g = next(e for e in entries if e["region"] is None)
        self.assertEqual(null_g["count"], 1)
        self.assertIsNone(null_g["amount_sum"])  # 全 NULL 组保持 NULL
        self.assertIsNone(null_g["amount_avg"])

    def test_empty_result_no_group_by(self):
        q = self.queries["global"]
        entries, _ = rollup.run(self.conn, self.model, q,
                                rollup.plan(self.model, q, self.conn))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["count"], 0)
        self.assertIsNone(entries[0]["amount_sum"])
        self.assertIsNone(entries[0]["amount_avg"])

    def test_empty_result_with_group_by(self):
        q = {"id": "t", "group_by": ["region"],
             "where": {"region": ["missing"]}, "measures": ["count"]}
        entries, _ = rollup.run(self.conn, self.model, q,
                                rollup.plan(self.model, q, self.conn))
        self.assertEqual(entries, [])  # 有 group_by 则空结果无组

    def test_null_in_allowed_set(self):
        q = {"id": "t", "group_by": ["day"], "where": {"region": [None]},
             "measures": ["count", "amount_sum", "amount_avg"]}
        entries, _ = rollup.run(self.conn, self.model, q,
                                rollup.plan(self.model, q, self.conn))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["day"], "2026-01-02")
        self.assertEqual(entries[0]["count"], 1)

    def test_fallback_to_fact(self):
        model2 = rollup.Model(self.model.fact, self.model.snapshot,
                              self.model.dimensions,
                              tuple(v for v in self.model.views
                                    if v.name in ("v_a", "v_avg", "v_stale")))
        q = {"id": "t", "group_by": ["day"], "where": {"product": ["B"]},
             "measures": ["count", "amount_avg"]}
        p = rollup.plan(model2, q, self.conn)
        self.assertTrue(p.fallback)
        self.assertEqual(p.chosen, "fact")
        self.assertIn("回退", p.fallback_reason)
        entries, tables = rollup.run(self.conn, model2, q, p)
        self.assertEqual(tables, {"fact"})
        direct, _ = rollup.direct_fact(self.conn, model2, q)
        ok, diffs = rollup.reconcile(entries, direct, q)
        self.assertTrue(ok, diffs)

    def test_reconcile_all_queries(self):
        for qid, q in self.queries.items():
            p = rollup.plan(self.model, q, self.conn)
            entries, _ = rollup.run(self.conn, self.model, q, p)
            direct, dt = rollup.direct_fact(self.conn, self.model, q)
            self.assertEqual(dt, {"fact"})
            ok, diffs = rollup.reconcile(entries, direct, q)
            self.assertTrue(ok, f"{qid}: {diffs}")

    def test_new_query_not_hardcoded(self):
        # 全新 query_id：走同一套计划逻辑，结果与直算一致
        q = {"id": "brand_new", "group_by": ["product"],
             "where": {"day": ["2026-01-01", "2026-01-02"]},
             "measures": ["count", "amount_sum", "amount_avg"]}
        p = rollup.plan(self.model, q, self.conn)
        self.assertEqual(p.chosen, "v_detail")
        entries, _ = rollup.run(self.conn, self.model, q, p)
        direct, _ = rollup.direct_fact(self.conn, self.model, q)
        ok, diffs = rollup.reconcile(entries, direct, q)
        self.assertTrue(ok, diffs)

    def test_invalid_query_rejected(self):
        with self.assertRaises(ValueError):
            rollup.plan(self.model, {"id": "x", "group_by": ["nope"],
                                     "where": {}, "measures": ["count"]},
                        self.conn)
        with self.assertRaises(ValueError):
            rollup.plan(self.model, {"id": "x", "group_by": [], "where": {},
                                     "measures": ["median"]}, self.conn)


if __name__ == "__main__":
    unittest.main()
