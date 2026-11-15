"""四个现成案例的规划/执行验收。

覆盖：已有顺序拼接(零Sort)、边界交错(插Sort)、删掉前导键(插Sort)、
保序改名后稳定过滤(零Sort)，并与独立字典分组逐组核对。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from etl.catalog import Catalog
from etl.planner import Planner, load_plans
from etl.runner import run_plan
from tests.helpers import dict_group

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"


class FixturePlansTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = Catalog(FIXTURES / "sources.json")
        cls.declarations = load_plans(FIXTURES / "plans.json")
        cls.plans = {
            decl["id"]: Planner(cls.catalog).plan(decl)
            for decl in cls.declarations
        }

    def _run(self, plan_id: str):
        planned = self.plans[plan_id]
        return run_plan(self.catalog, planned["executable_plan"])

    def test_already_grouped_has_zero_sorts(self):
        planned = self.plans["already_grouped"]
        self.assertEqual(planned["sort_count"], 0)
        rows, report = self._run("already_grouped")
        self.assertEqual(report["sort_call_count"], 0)
        concat_record = next(r for r in report["node_records"] if r["op"] == "concat")
        self.assertTrue(concat_record["detail"]["boundary_checks"])
        self.assertEqual(
            rows,
            [
                {"region": "A", "count": 2, "sum": 30},
                {"region": "B", "count": 2, "sum": 30},
            ],
        )

    def test_crossing_inserts_real_sort(self):
        planned = self.plans["crossing"]
        self.assertEqual(planned["sort_count"], 1)
        sort_info = planned["injected_sorts"][0]
        self.assertEqual(sort_info["injected_for"], "g")
        self.assertEqual([k["field"] for k in sort_info["keys"]], ["region", "tick"])
        # concat 自身必须留下失效原因，而不是凭相同 order 字段继承。
        concat_node = next(n for n in planned["nodes"] if n["id"] == "u")
        self.assertEqual(concat_node["derived_order"], [])
        self.assertIn("边界倒退", concat_node["invalid_reason"])

        rows, report = self._run("crossing")
        self.assertEqual(report["sort_call_count"], 1)
        sort_record = next(r for r in report["node_records"] if r["op"] == "sort")
        self.assertEqual(sort_record["input_rows"], 4)
        self.assertEqual(sort_record["output_rows"], 4)
        self.assertTrue(sort_record["sort_call"]["stable"])

    def test_drop_leading_key_kills_order_until_sort(self):
        planned = self.plans["drop_leading"]
        project_node = next(n for n in planned["nodes"] if n["id"] == "p")
        self.assertEqual(project_node["derived_order"], [])
        self.assertIn("前导排序键", project_node["invalid_reason"])
        self.assertEqual(planned["sort_count"], 1)
        self.assertEqual(
            planned["injected_sorts"][0]["keys"],
            [{"field": "t", "direction": "asc", "nulls": "first"}],
        )
        rows, report = self._run("drop_leading")
        self.assertEqual(report["sort_call_count"], 1)
        self.assertEqual([r["t"] for r in rows], [1, 2, 3, 4])

    def test_rename_then_filter_keeps_order_without_sort(self):
        planned = self.plans["rename"]
        self.assertEqual(planned["sort_count"], 0)
        filter_node = next(n for n in planned["nodes"] if n["id"] == "f")
        self.assertEqual(
            [s["field"] for s in filter_node["derived_order"]],
            ["area", "time"],
        )
        rows, report = self._run("rename")
        self.assertEqual(report["sort_call_count"], 0)
        filter_record = next(r for r in report["node_records"] if r["op"] == "filter")
        self.assertEqual((filter_record["input_rows"], filter_record["output_rows"]), (2, 1))
        self.assertEqual(rows, [{"area": "A", "count": 1, "sum": 20}])

    def test_every_group_matches_independent_dict_aggregation(self):
        def load_jsonl(name: str):
            with (FIXTURES / f"{name}.jsonl").open(encoding="utf-8") as handle:
                return [json.loads(line) for line in handle]

        raw_inputs = {name: load_jsonl(name) for name in ("left", "right", "overlap")}
        cases = {
            "already_grouped": (
                raw_inputs["left"] + raw_inputs["right"],
                ["region"],
                "amount",
            ),
            "crossing": (
                raw_inputs["left"] + raw_inputs["overlap"],
                ["region", "tick"],
                "amount",
            ),
            "drop_leading": (
                raw_inputs["left"] + raw_inputs["right"],
                ["tick"],
                "amount",
            ),
            "rename": (
                [r for r in raw_inputs["left"] if r["amount"] >= 15],
                ["region"],
                "amount",
            ),
        }
        for plan_id, (rows, keys, sum_field) in cases.items():
            with self.subTest(plan=plan_id):
                output_rows, _ = self._run(plan_id)
                expected = dict_group(rows, keys, sum_field)
                if plan_id == "drop_leading":
                    for row in expected:
                        row["t"] = row.pop("tick")
                if plan_id == "rename":
                    for row in expected:
                        row["area"] = row.pop("region")
                self.assertEqual(output_rows, expected)

    def test_group_outputs_remain_in_key_order(self):
        for plan_id in ("already_grouped", "crossing", "drop_leading"):
            with self.subTest(plan=plan_id):
                rows, _ = self._run(plan_id)
                key_fields = list(k for k in rows[0].keys() if k not in ("count", "sum"))
                keys_as_tuples = [tuple(r[k] for k in key_fields) for r in rows]
                self.assertEqual(
                    keys_as_tuples,
                    sorted(keys_as_tuples, key=lambda t: tuple((v is not None, v) for v in t)),
                )


if __name__ == "__main__":
    unittest.main()
