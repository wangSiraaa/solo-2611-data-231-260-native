"""引擎语义单元测试：TOAST 继承、K 键语义、schema 演进边界与列位置不固化。

全部用独立编码器合成消息，在内存 SQLite 上驱动 Mirror，重点验证那些
只靠真实 fixture 覆盖不到的失败路径与边界规则。
"""

from __future__ import annotations

import sqlite3
import unittest

from pgmirror.contract import load_contract
from pgmirror.driver import run
from pgmirror.errors import UnsupportedFeatureError
from tests import pgoutput_builder as pb
from pathlib import Path

CONTRACT = Path(__file__).resolve().parent.parent / "contract.json"

COLS_V1 = [(1, "id", 23, -1), (0, "body", 25, -1), (0, "qty", 23, -1)]
COLS_V2 = COLS_V1 + [(0, "note", 25, -1)]


def _contract():
    return load_contract(CONTRACT)


def _run(messages):
    db = sqlite3.connect(":memory:")
    result = run(pb.stream(messages), _contract(), db)
    return db, result


def _begin_commit(inner_messages, xid=100, lsn=0x1000):
    return [
        pb.begin(lsn, 1, xid),
        *inner_messages,
        pb.commit(lsn, lsn + 0x30, 2),
    ]


def _rows(db):
    db.row_factory = sqlite3.Row
    return [dict(r) for r in db.execute("SELECT * FROM records ORDER BY id")]


class ToastInheritanceTests(unittest.TestCase):
    def test_u_inherits_existing_value_across_updates(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("toast-body"), pb.text_value("10")]),
        ])
        # 第二个事务：只改 qty，body 发 u，必须继承旧值。
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.update(1, [pb.text_value("1"), pb.unchanged_value(), pb.text_value("11")]),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        rows = _rows(db)
        self.assertEqual(rows[0]["body"], "toast-body")
        self.assertEqual(rows[0]["qty"], 11)

    def test_u_without_existing_row_fails_and_aborts(self):
        # 更新一条不存在的行且带 u：无旧值可继承，事务整体失败。
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.update(1, [pb.text_value("9"), pb.unchanged_value(), pb.text_value("1")]),
        ])
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIsInstance(result.fatal["message"], str)
        self.assertEqual(result.transactions[0]["status"], "aborted")
        # 业务表未创建 -> 没有任何泄漏。
        self.assertEqual(
            db.execute("SELECT count(*) FROM sqlite_master WHERE name='records'").fetchone()[0],
            0,
        )

    def test_u_in_insert_is_illegal(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.unchanged_value(), pb.text_value("10")]),
        ])
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("'u' is illegal in an Insert", result.fatal["message"])

    def test_null_marker_is_real_null_not_inherited(self):
        # n 是真 NULL：即使同列在旧行有值，也必须被清空。
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("keep"), pb.text_value("10")]),
        ])
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.update(1, [pb.text_value("1"), pb.null_value(), pb.text_value("10")]),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        self.assertIsNone(_rows(db)[0]["body"])


class KeyTupleTests(unittest.TestCase):
    def _seed(self):
        return _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
            pb.insert(1, [pb.text_value("2"), pb.text_value("b"), pb.text_value("20")]),
        ])

    def test_k_locates_old_primary_key_and_moves_row(self):
        messages = self._seed()
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.update(
                1,
                [pb.text_value("3"), pb.text_value("b"), pb.text_value("20")],
                old_key_values=[pb.text_value("2"), pb.null_value(), pb.null_value()],
            ),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        ids = [r["id"] for r in _rows(db)]
        self.assertEqual(ids, [1, 3])  # 旧 id=2 行不存在，没有残留。

    def test_k_non_key_null_does_not_clear_business_fields(self):
        messages = self._seed()
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            # K 里非键列是 n；新 tuple 只带键和 qty，body 用 u 继承。
            pb.update(
                1,
                [pb.text_value("2"), pb.unchanged_value(), pb.text_value("21")],
                old_key_values=[pb.text_value("2"), pb.null_value(), pb.null_value()],
            ),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        row = next(r for r in _rows(db) if r["id"] == 2)
        self.assertEqual(row["body"], "b")  # 没被 K 里的 n 清空
        self.assertEqual(row["qty"], 21)

    def test_delete_uses_k_and_requires_existing_row(self):
        messages = self._seed()
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.delete(1, [pb.text_value("2"), pb.null_value(), pb.null_value()]),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        self.assertEqual([r["id"] for r in _rows(db)], [1])

    def test_delete_missing_row_fails(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.delete(1, [pb.text_value("99"), pb.null_value(), pb.null_value()]),
        ])
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("does not exist", result.fatal["message"])

    def test_update_without_k_uses_new_tuple_key(self):
        messages = self._seed()
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            # 无 K：新 tuple 直接以 id=1 定位并改 qty。
            pb.update(1, [pb.text_value("1"), pb.unchanged_value(), pb.text_value("99")]),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        row = next(r for r in _rows(db) if r["id"] == 1)
        self.assertEqual(row["qty"], 99)
        self.assertEqual(row["body"], "a")


class SchemaEvolutionTests(unittest.TestCase):
    def test_append_nullable_column_backfills_null(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
        ])
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V2),  # 尾部追加 note
            pb.update(
                1,
                [pb.text_value("1"), pb.unchanged_value(),
                 pb.text_value("10"), pb.null_value()],
            ),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        row = _rows(db)[0]
        self.assertIn("note", row)
        self.assertIsNone(row["note"])  # 旧行新列为空

    def test_column_type_change_is_unsupported(self):
        # 第二版把 body 的 OID 从 text(25) 改成 int4(23)。
        bad_cols = [(1, "id", 23, -1), (0, "body", 23, -1), (0, "qty", 23, -1)]
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
        ])
        messages += _begin_commit([
            pb.relation(1, "public", "records", bad_cols),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertEqual(result.fatal["error_type"], "UnsupportedFeatureError")
        self.assertIn("incompatible schema change", result.fatal["message"])

    def test_column_rename_is_unsupported(self):
        bad_cols = [(1, "id", 23, -1), (0, "body2", 25, -1), (0, "qty", 23, -1)]
        messages = _begin_commit([pb.relation(1, "public", "records", COLS_V1)])
        messages += _begin_commit([pb.relation(1, "public", "records", bad_cols)],
                                  xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("incompatible schema change", result.fatal["message"])

    def test_column_drop_is_unsupported(self):
        messages = _begin_commit([pb.relation(1, "public", "records", COLS_V2)])
        messages += _begin_commit([pb.relation(1, "public", "records", COLS_V1)],
                                  xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("dropping columns", result.fatal["message"])

    def test_unsupported_type_oid_rejected(self):
        # OID 16 = bool，不在 contract 支持范围内。
        bad_cols = [(1, "id", 23, -1), (0, "flag", 16, -1)]
        messages = _begin_commit([pb.relation(1, "public", "records", bad_cols)])
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertEqual(result.fatal["error_type"], "UnsupportedFeatureError")

    def test_tuple_column_count_mismatch_rejected(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a")]),  # 少一列
        ])
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("tuple has 2 columns but Relation", result.fatal["message"])


class ColumnPositionTests(unittest.TestCase):
    def test_tuple_aligned_by_latest_relation_not_first_seen_position(self):
        """不能把首次见到的列位置永久固定：追加列后按新 Relation 对齐。"""

        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
        ])
        # 追加 note 后，新 tuple 第 4 个位置必须解释成 note，而不是错位到旧列。
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V2),
            pb.update(
                1,
                [pb.text_value("1"), pb.unchanged_value(),
                 pb.text_value("10"), pb.text_value("note-value")],
            ),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        row = _rows(db)[0]
        self.assertEqual(row["note"], "note-value")
        self.assertEqual(row["body"], "a")
        self.assertEqual(row["qty"], 10)


    def test_column_position_reattach_after_append_within_same_txn(self):
        """同一事务内先按 3 列插入、再 Relation 追加列、再按 4 列更新。"""

        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
            # 同事务重发扩展后的 Relation，随后 tuple 按 4 列解释。
            pb.relation(1, "public", "records", COLS_V2),
            pb.update(
                1,
                [pb.text_value("1"), pb.unchanged_value(),
                 pb.text_value("10"), pb.text_value("late-note")],
            ),
        ])
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        row = _rows(db)[0]
        self.assertEqual(row["note"], "late-note")
        self.assertEqual(row["body"], "a")


class IntegrityAndResendTests(unittest.TestCase):
    def test_duplicate_insert_rejected(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
            pb.insert(1, [pb.text_value("1"), pb.text_value("b"), pb.text_value("11")]),
        ])
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("violates primary key", result.fatal["message"])
        self.assertEqual(result.transactions[0]["status"], "aborted")

    def test_primary_key_change_collision_rejected(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
            pb.insert(1, [pb.text_value("2"), pb.text_value("b"), pb.text_value("20")]),
        ])
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.update(
                1,
                [pb.text_value("2"), pb.unchanged_value(), pb.text_value("99")],
                old_key_values=[pb.text_value("1"), pb.null_value(), pb.null_value()],
            ),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("already exists on another row", result.fatal["message"])

    def test_key_column_with_null_marker_rejected(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
        ])
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.delete(1, [pb.null_value(), pb.null_value(), pb.null_value()]),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("must carry a text value", result.fatal["message"])

    def test_identical_relation_resend_is_noop(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
        ])
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),  # 与已提交定义逐字段相同
            pb.update(1, [pb.text_value("1"), pb.unchanged_value(), pb.text_value("11")]),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        self.assertEqual(_rows(db)[0]["qty"], 11)

    def test_failed_txn_leaves_prior_commit_intact(self):
        # 第一个事务提交成功，第二个事务非法：已提交行必须原样保留。
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
        ])
        messages += _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("1"), pb.text_value("dup"), pb.text_value("11")]),
        ], xid=101, lsn=0x2000)
        db, result = _run(messages)
        self.assertFalse(result.ok)
        rows = _rows(db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qty"], 10)  # 未被失败事务污染


class Int4HandlingTests(unittest.TestCase):
    def test_int4_parsed_as_integer_and_range_checked(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("42"), pb.text_value("a"), pb.text_value("-7")]),
        ])
        db, result = _run(messages)
        self.assertTrue(result.ok, result.fatal)
        row = _rows(db)[0]
        self.assertIsInstance(row["id"], int)
        self.assertEqual(row["id"], 42)
        self.assertEqual(row["qty"], -7)

    def test_non_integer_text_in_int4_column_rejected(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value("oops"), pb.text_value("a"), pb.text_value("10")]),
        ])
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("expected int4", result.fatal["message"])

    def test_int4_out_of_range_rejected(self):
        messages = _begin_commit([
            pb.relation(1, "public", "records", COLS_V1),
            pb.insert(1, [pb.text_value(str(2 ** 31)), pb.text_value("a"), pb.text_value("10")]),
        ])
        db, result = _run(messages)
        self.assertFalse(result.ok)
        self.assertIn("out of int4 range", result.fatal["message"])


if __name__ == "__main__":
    unittest.main()
