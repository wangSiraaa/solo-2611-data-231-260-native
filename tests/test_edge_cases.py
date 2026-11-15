"""边界与安全性质测试：虚假声明、空源、NULL 语义、稳定排序、顺序守卫。"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from etl.errors import ExecutionError, OrderingError, PlanError
from etl.ops import stream_groups
from etl.ordering import (
    compare_rows,
    required_group_specs,
    stable_sort,
)
from tests.helpers import dict_group, execute, write_source


NULLABLE_SCHEMA = {
    "id": "int",
    "region": "text?",
    "tick": "int",
    "amount": "int?",
}


def group_plan(keys: list[str], sum_field: str = "amount", source: str = "s"):
    return {
        "id": "t",
        "output": "g",
        "nodes": [
            {"id": "src", "op": "source", "source": source},
            {"id": "g", "op": "stream_group", "input": "src",
             "keys": keys, "sum": sum_field},
        ],
    }


class TamperedDeclarationTest(unittest.TestCase):
    def test_unsorted_rows_fail_against_declared_order(self):
        tmp = self._tmp()
        rows = [
            {"id": 1, "region": "B", "tick": 1, "amount": 10},
            {"id": 2, "region": "A", "tick": 2, "amount": 20},
        ]
        sources_path = write_source(tmp, "s", rows)
        plan = group_plan(["region"])
        with self.assertRaisesRegex(OrderingError, "倒退"):
            execute(tmp, sources_path, plan)

    def test_tampering_also_fails_at_runtime_even_if_planner_skipped(self):
        # 直接构造 Catalog 读取被篡改文件，执行期核验同样失败。
        tmp = self._tmp()
        sources_path = write_source(
            tmp,
            "s",
            [
                {"id": 1, "region": "A", "tick": 2, "amount": 1},
                {"id": 2, "region": "A", "tick": 1, "amount": 2},
            ],
        )
        from etl.catalog import Catalog

        with self.assertRaises(OrderingError):
            Catalog(sources_path).load("s")

    def _tmp(self) -> Path:
        import tempfile

        return Path(tempfile.mkdtemp(prefix="etl-tamper-"))


class EmptyAndNullTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="etl-edge-"))

    def test_empty_source_produces_no_groups_and_no_sort(self):
        sources_path = write_source(self.tmp, "empty", [])
        plan = group_plan(["region"], source="empty")
        planned, rows, report = execute(self.tmp, sources_path, plan)
        self.assertEqual(rows, [])
        self.assertEqual(planned["sort_count"], 0)
        self.assertEqual(report["sort_call_count"], 0)
        source_record = report["node_records"][0]
        self.assertEqual(source_record["output_rows"], 0)

    def test_all_filtered_out_input_still_groups_to_empty(self):
        rows = [
            {"id": 1, "region": "A", "tick": 1, "amount": 10},
            {"id": 2, "region": "B", "tick": 2, "amount": 5},
        ]
        sources_path = write_source(self.tmp, "s", rows)
        plan = {
            "id": "t",
            "output": "g",
            "nodes": [
                {"id": "src", "op": "source", "source": "s"},
                {"id": "f", "op": "filter", "input": "src",
                 "field": "amount", "ge": 100},
                {"id": "g", "op": "stream_group", "input": "f",
                 "keys": ["region"], "sum": "amount"},
            ],
        }
        planned, out_rows, report = execute(self.tmp, sources_path, plan)
        self.assertEqual(out_rows, [])
        self.assertEqual(planned["sort_count"], 0)
        filter_record = next(r for r in report["node_records"] if r["op"] == "filter")
        self.assertEqual(filter_record["output_rows"], 0)

    def test_all_null_sum_group_emits_null_and_mixed_group_ignores_nulls(self):
        rows = [
            {"id": 1, "region": "A", "tick": 1, "amount": None},
            {"id": 2, "region": "A", "tick": 2, "amount": None},
            {"id": 3, "region": "B", "tick": 1, "amount": None},
            {"id": 4, "region": "B", "tick": 2, "amount": 7},
        ]
        sources_path = write_source(self.tmp, "s", rows)
        planned, out_rows, report = execute(self.tmp, sources_path, group_plan(["region"]))
        self.assertEqual(
            out_rows,
            [
                {"region": "A", "count": 2, "sum": None},
                {"region": "B", "count": 2, "sum": 7},
            ],
        )
        self.assertEqual(out_rows, dict_group(rows, ["region"], "amount"))

    def test_null_key_is_groupable_and_sorts_first(self):
        rows = [
            {"id": 1, "region": None, "tick": 1, "amount": 3},
            {"id": 2, "region": None, "tick": 2, "amount": 4},
            {"id": 3, "region": "A", "tick": 1, "amount": 5},
        ]
        sources_path = write_source(self.tmp, "s", rows, schema=NULLABLE_SCHEMA)
        planned, out_rows, report = execute(self.tmp, sources_path, group_plan(["region"]))
        self.assertEqual(
            out_rows,
            [
                {"region": None, "count": 2, "sum": 7},
                {"region": "A", "count": 1, "sum": 5},
            ],
        )
        self.assertEqual(planned["sort_count"], 0)

    def test_null_key_unsorted_input_gets_real_sort_then_correct_groups(self):
        rows = [
            {"id": 1, "region": "A", "tick": 1, "amount": 5},
            {"id": 2, "region": None, "tick": 2, "amount": 3},
            {"id": 3, "region": None, "tick": 3, "amount": 4},
        ]
        # 声明仅按 tick 排序，使 region 前缀无法被证明 -> 必须插 Sort。
        sources_path = write_source(
            self.tmp,
            "s",
            rows,
            order=[{"field": "tick", "direction": "asc", "nulls": "first"}],
            schema=NULLABLE_SCHEMA,
        )
        planned, out_rows, report = execute(self.tmp, sources_path, group_plan(["region"]))
        self.assertEqual(planned["sort_count"], 1)
        self.assertEqual(report["sort_call_count"], 1)
        self.assertEqual(
            out_rows,
            [
                {"region": None, "count": 2, "sum": 7},
                {"region": "A", "count": 1, "sum": 5},
            ],
        )
        self.assertEqual(out_rows, dict_group(rows, ["region"], "amount"))


class OrderingSemanticsTest(unittest.TestCase):
    def test_desc_keeps_null_position_independent(self):
        specs = [{"field": "tick", "direction": "desc", "nulls": "first"}]
        rows = [
            {"tick": None},
            {"tick": 3},
            {"tick": 1},
        ]
        ordered = stable_sort(
            [{"tick": 1}, {"tick": None}, {"tick": 3}], specs
        )
        self.assertEqual(ordered, rows)
        # NULLS LAST + DESC 时 NULL 必须落到末尾。
        specs_last = [{"field": "tick", "direction": "desc", "nulls": "last"}]
        ordered_last = stable_sort(
            [{"tick": None}, {"tick": 1}, {"tick": 3}], specs_last
        )
        self.assertEqual([r["tick"] for r in ordered_last], [3, 1, None])

    def test_stable_sort_preserves_relative_order_of_equal_keys(self):
        specs = [{"field": "region", "direction": "asc", "nulls": "first"}]
        rows = [
            {"id": 1, "region": "B"},
            {"id": 2, "region": "A"},
            {"id": 3, "region": "A"},
            {"id": 4, "region": "B"},
        ]
        ordered = stable_sort(rows, specs)
        self.assertEqual([r["id"] for r in ordered], [2, 3, 1, 4])

    def test_stream_group_guard_rejects_split_group(self):
        # 同键行被拆开：若用哈希聚合会悄悄合并，连续组实现必须直接报错。
        rows = [
            {"region": "A", "amount": 1},
            {"region": "B", "amount": 2},
            {"region": "A", "amount": 3},
        ]
        with self.assertRaises(ExecutionError):
            list(stream_groups(rows, ["region"], "amount"))

    def test_compare_rows_desc_direction(self):
        specs = [{"field": "v", "direction": "desc", "nulls": "last"}]
        self.assertEqual(compare_rows({"v": 2}, {"v": 1}, specs), -1)
        self.assertEqual(compare_rows({"v": 1}, {"v": 2}, specs), 1)
        self.assertEqual(compare_rows({"v": 1}, {"v": 1}, specs), 0)

    def test_required_prefix_mismatch_reasons(self):
        from etl.ordering import satisfies_prefix

        actual = [{"field": "a", "direction": "desc", "nulls": "first"}]
        required = required_group_specs(["a"])
        ok, reason = satisfies_prefix(actual, required)
        self.assertFalse(ok)
        self.assertIn("方向", reason)

        ok, reason = satisfies_prefix([], required)
        self.assertFalse(ok)
        self.assertIn("缺少排序键", reason)


class ProjectAndConcatRulesTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="etl-rule-"))

    def test_project_output_columns_follow_source_order_not_mapping_order(self):
        rows = [{"id": 1, "region": "A", "tick": 1, "amount": 10}]
        sources_path = write_source(self.tmp, "s", rows)
        plan = {
            "id": "t",
            "output": "p",
            "nodes": [
                {"id": "src", "op": "source", "source": "s"},
                {"id": "p", "op": "project", "input": "src",
                 "mapping": {"amount": "amount", "area": "region"}},
            ],
        }
        planned, out_rows, report = execute(self.tmp, sources_path, plan)
        # 输出列名按来源列原顺序：region(area) 在 amount 前。
        self.assertEqual(list(out_rows[0].keys()), ["area", "amount"])

    def test_desc_declared_source_feeding_asc_group_requires_sort(self):
        rows = [
            {"id": 1, "region": "B", "tick": 1, "amount": 1},
            {"id": 2, "region": "A", "tick": 2, "amount": 2},
        ]
        sources_path = write_source(
            self.tmp,
            "s",
            rows,
            order=[{"field": "region", "direction": "desc", "nulls": "first"}],
        )
        planned, out_rows, report = execute(self.tmp, sources_path, group_plan(["region"]))
        self.assertEqual(planned["sort_count"], 1)
        self.assertEqual([r["region"] for r in out_rows], ["A", "B"])

    def test_concat_boundary_regression_is_runtime_checked_when_claim_proven(self):
        # 两个内部有序源，构造边界倒退；规划器应标 unproven 并由 Sort 兜底，
        # 运行不会抛边界异常（责任转移给显式 Sort）。
        left = [
            {"id": 1, "region": "A", "tick": 3, "amount": 1},
        ]
        right = [
            {"id": 2, "region": "A", "tick": 1, "amount": 2},
        ]
        sp1 = write_source(self.tmp, "l", left, data_name="l")
        sources_path = write_source(self.tmp, "r", right, data_name="r")
        self.assertTrue(Path(sp1).exists())
        declarations = json.loads(Path(sources_path).read_text(encoding="utf-8"))
        self.assertEqual(set(declarations), {"l", "r"})
        plan = {
            "id": "t",
            "output": "g",
            "nodes": [
                {"id": "l", "op": "source", "source": "l"},
                {"id": "r", "op": "source", "source": "r"},
                {"id": "u", "op": "concat", "inputs": ["l", "r"]},
                {"id": "g", "op": "stream_group", "input": "u",
                 "keys": ["region", "tick"], "sum": "amount"},
            ],
        }
        planned, out_rows, report = execute(self.tmp, sources_path, plan)
        concat_node = next(n for n in planned["nodes"] if n["id"] == "u")
        self.assertEqual(concat_node["derived_order"], [])
        self.assertEqual(planned["sort_count"], 1)
        self.assertEqual([(r["region"], r["tick"]) for r in out_rows],
                         [("A", 1), ("A", 3)])

    def test_concat_skips_empty_middle_input_when_checking_boundaries(self):
        left = [{"id": 1, "region": "A", "tick": 1, "amount": 1}]
        middle: list[dict] = []
        right = [{"id": 2, "region": "B", "tick": 1, "amount": 2}]
        write_source(self.tmp, "l", left, data_name="l")
        write_source(self.tmp, "m", middle, data_name="m")
        sources_path = write_source(self.tmp, "r", right, data_name="r")
        plan = {
            "id": "t",
            "output": "g",
            "nodes": [
                {"id": "l", "op": "source", "source": "l"},
                {"id": "m", "op": "source", "source": "m"},
                {"id": "r", "op": "source", "source": "r"},
                {"id": "u", "op": "concat", "inputs": ["l", "m", "r"]},
                {"id": "g", "op": "stream_group", "input": "u",
                 "keys": ["region"], "sum": "amount"},
            ],
        }
        planned, out_rows, report = execute(self.tmp, sources_path, plan)
        concat_node = next(n for n in planned["nodes"] if n["id"] == "u")
        self.assertEqual(planned["sort_count"], 0)
        self.assertTrue(concat_node["evidence"])
        self.assertEqual([r["region"] for r in out_rows], ["A", "B"])

    def test_unknown_filter_predicate_rejected(self):
        rows = [{"id": 1, "region": "A", "tick": 1, "amount": 10}]
        sources_path = write_source(self.tmp, "s", rows)
        plan = {
            "id": "t",
            "output": "f",
            "nodes": [
                {"id": "src", "op": "source", "source": "s"},
                {"id": "f", "op": "filter", "input": "src",
                 "field": "amount", "like": "%x"},
            ],
        }
        with self.assertRaises(PlanError):
            execute(self.tmp, sources_path, plan)

    def test_type_mismatch_in_data_rejected(self):
        bad_rows = [{"id": "x", "region": "A", "tick": 1, "amount": 10}]
        sources_path = write_source(self.tmp, "s", bad_rows)
        from etl.catalog import Catalog

        with self.assertRaises(OrderingError):
            Catalog(sources_path).load("s")


if __name__ == "__main__":
    unittest.main()
