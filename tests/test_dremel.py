"""针对性自动验证（纯标准库 unittest，无第三方依赖）。

覆盖：
1. 真实材料逐行规范化 JSON 往返；
2. null list / 空 list / null 元素 / 缺失 meta / 空字符串 / 不同内层长度的编码差别；
3. 破坏某叶 r/d、删除共享边界 -> 解码拒绝；
4. 严格 schema：额外字段、required null、类型错误、缺必填拒绝；
5. 栈式主解码器与递归参考解码器交叉一致；
6. 多叶共享重复祖先形状矛盾、记录数不一致、多余列拒绝。

直接运行：python -m tests.test_dremel   或   python tests/test_dremel.py
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from dremel.decode import DecodeError, StackDecoder, decode_columns
from dremel.encode import ColumnEncoder
from dremel.metadata import build_meta
from dremel.planner import compile_plans
from dremel.reference import reference_decode
from dremel.schema import RowValidationError, load_schema, validate_and_normalize

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = load_schema(json.loads((ROOT / "schema.json").read_text(encoding="utf-8")))


def _load_jsonl(p: Path) -> list:
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


class DremelTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.rows = _load_jsonl(ROOT / "fixtures" / "rows.jsonl")
        cls.enc = ColumnEncoder(SCHEMA)
        cls.normalized = cls.enc.encode_all(cls.rows)
        cls.plans = compile_plans(SCHEMA)

    def col(self, *parts: str) -> list[dict]:
        return self.enc.columns[tuple(parts)]

    def decode(self, cols=None):
        return decode_columns(SCHEMA, cols if cols is not None else self.enc.columns)

    # ---------------- 1) 真实材料逐行规范化 JSON 往返 ---------------- #
    def test_roundtrip_normalized_json_line_by_line(self):
        restored = self.decode()
        self.assertEqual(len(restored), len(self.normalized))
        for orig, got in zip(self.normalized, restored):
            self.assertEqual(
                json.dumps(orig, ensure_ascii=False, sort_keys=True),
                json.dumps(got, ensure_ascii=False, sort_keys=True),
            )

    def test_reference_decoder_matches_stack(self):
        for path, entries in self.enc.columns.items():
            self.assertEqual(
                StackDecoder(self.plans[path], path, entries).run(),
                reference_decode(self.plans[path], entries),
            )

    # ---------------- 2) 边界结构的编码差别 ---------------- #
    def test_null_list_empty_list_null_element(self):
        code, values = self.col("batches", "code"), self.col("batches", "values")
        self.assertEqual(code[0], {"r": 0, "d": 0})          # null list
        self.assertEqual(code[1], {"r": 0, "d": 1})          # 空 list
        self.assertEqual(code[2], {"r": 0, "d": 2})          # null 元素
        self.assertEqual(values[0], {"r": 0, "d": 0})
        self.assertEqual(values[1], {"r": 0, "d": 1})
        self.assertEqual(values[2], {"r": 0, "d": 2})
        self.assertTrue(all("value" not in e for e in code[:3]))

    def test_missing_meta_normalizes_to_null(self):
        name = self.col("meta", "name")
        self.assertEqual(name[0], {"r": 0, "d": 0})  # 显式 null
        self.assertEqual(name[4], {"r": 0, "d": 0})  # 缺失
        self.assertIsNone(self.normalized[0]["meta"])
        self.assertIsNone(self.normalized[4]["meta"])

    def test_empty_string_is_real_value(self):
        self.assertEqual(self.col("meta", "name")[2], {"r": 0, "d": 2, "value": ""})

    def test_inner_list_length_difference(self):
        values = self.col("batches", "values")
        self.assertEqual([e["r"] for e in values[5:10]], [0, 2, 1, 2, 2])
        b1, b2 = self.decode()[4]["batches"]
        self.assertEqual(b1["values"], [None, 7])
        self.assertEqual(b2["values"], [8, None, 9])

    def test_every_record_marks_every_leaf(self):
        for entries in self.enc.columns.values():
            self.assertEqual(sum(1 for e in entries if e["r"] == 0), len(self.normalized))

    def test_value_only_at_max_definition(self):
        for path, entries in self.enc.columns.items():
            max_d = self.plans[path].max_d
            for e in entries:
                if "value" in e:
                    self.assertEqual(e["d"], max_d)
                else:
                    self.assertLess(e["d"], max_d)

    def test_empty_struct_distinct_from_null(self):
        self.assertEqual(self.col("meta", "name")[1], {"r": 0, "d": 1})
        self.assertEqual(self.normalized[1]["meta"], {"name": None})
        self.assertIsNone(self.normalized[0]["meta"])

    # ---------------- 3) 破坏 r/d 与删除共享边界 ---------------- #
    def _corrupt(self, path, index, **patch):
        cols = {p: [dict(e) for e in es] for p, es in self.enc.columns.items()}
        cols[path][index].update(patch)
        return cols

    def test_corrupt_repetition_level_rejected(self):
        with self.assertRaises(DecodeError):
            self.decode(self._corrupt(("batches", "code"), 4, r=5))

    def test_corrupt_definition_level_rejected(self):
        with self.assertRaises(DecodeError):
            self.decode(self._corrupt(("batches", "values"), 6, d=99))

    def test_value_at_non_max_d_rejected(self):
        cols = {p: [dict(e) for e in es] for p, es in self.enc.columns.items()}
        cols[("meta", "name")][0] = {"r": 0, "d": 1, "value": "x"}  # max_d=2
        with self.assertRaises(DecodeError):
            self.decode(cols)

    def test_missing_value_at_max_d_rejected(self):
        cols = {p: [dict(e) for e in es] for p, es in self.enc.columns.items()}
        cols[("id",)][0] = {"r": 0, "d": 0}  # id max_d=0 却无 value
        with self.assertRaises(DecodeError):
            self.decode(cols)

    def test_delete_shared_boundary_rejected(self):
        cols = {p: [dict(e) for e in es] for p, es in self.enc.columns.items()}
        del cols[("batches", "code")][4]  # 删掉 r=1 的 'b' 共享边界
        with self.assertRaises(DecodeError):
            self.decode(cols)

    def test_r_greater_than_d_rejected(self):
        with self.assertRaises(DecodeError):
            self.decode(self._corrupt(("batches", "values"), 6, r=6, d=2))

    # ---------------- 4) 严格 schema 校验 ---------------- #
    def test_extra_field_rejected(self):
        with self.assertRaises(RowValidationError):
            validate_and_normalize(SCHEMA, {"id": 1, "ghost": 1}, name="<root>")

    def test_required_null_rejected(self):
        for row in _load_jsonl(ROOT / "fixtures" / "invalid-row.jsonl"):
            with self.assertRaises(RowValidationError):
                validate_and_normalize(SCHEMA, row, name="<root>")

    def test_wrong_type_rejected(self):
        with self.assertRaises(RowValidationError):
            validate_and_normalize(SCHEMA, {"id": "x"}, name="<root>")
        with self.assertRaises(RowValidationError):
            validate_and_normalize(SCHEMA, {"id": 1, "meta": "x"}, name="<root>")

    def test_missing_required_rejected(self):
        with self.assertRaises(RowValidationError):
            validate_and_normalize(SCHEMA, {"meta": None}, name="<root>")

    def test_encode_rejects_invalid_row(self):
        bad = _load_jsonl(ROOT / "fixtures" / "invalid-row.jsonl")[0]
        with self.assertRaises(RowValidationError):
            ColumnEncoder(SCHEMA).add_row(bad)

    # ---------------- 5/6) 列间一致性与元数据 ---------------- #
    def test_record_count_mismatch_rejected(self):
        cols = {p: [dict(e) for e in es] for p, es in self.enc.columns.items()}
        cols[("id",)].pop(0)
        with self.assertRaises(DecodeError):
            self.decode(cols)

    def test_shared_ancestor_shape_conflict_rejected(self):
        cols = {p: [dict(e) for e in es] for p, es in self.enc.columns.items()}
        cols[("batches", "code")].insert(4, {"r": 1, "d": 3, "value": "zzz"})
        with self.assertRaises(DecodeError):
            self.decode(cols)

    def test_extra_column_rejected(self):
        cols = {p: [dict(e) for e in es] for p, es in self.enc.columns.items()}
        cols[("ghost",)] = [{"r": 0, "d": 0, "value": 1}]
        with self.assertRaises(DecodeError):
            self.decode(cols)

    def test_meta_thresholds_and_max_levels(self):
        meta = build_meta(self.enc.schema, self.enc.plans)
        by_path = {tuple(c["leaf_path"]): c for c in meta["columns"]}
        self.assertEqual(by_path[("batches", "values")]["max_repetition_level"], 2)
        self.assertEqual(by_path[("batches", "values")]["max_definition_level"], 6)
        self.assertEqual(by_path[("id",)]["max_repetition_level"], 0)
        self.assertEqual(by_path[("id",)]["max_definition_level"], 0)
        meanings = " ".join(
            t["meaning"] for t in by_path[("batches", "code")]["definition_thresholds"]
        )
        self.assertIn("null", meanings)
        self.assertIn("空 list", meanings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
