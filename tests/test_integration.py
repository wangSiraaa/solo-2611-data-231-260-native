"""端到端集成测试：真实 fixtures + 独立手工基准的全字段比对。

基准数据完全按 source.sql 的语义手工推导（md5 串独立重算），
不重新执行 source.sql，也不复用被测解析器的输出，因此构成独立来源比对。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pgmirror.contract import load_contract
from pgmirror.driver import build_outputs, inspect_at, run
from tests import pgoutput_builder as pb

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"
CONTRACT = ROOT / "contract.json"


def expected_final_state():
    """按 source.sql 四个事务推导的最终镜像（唯一存活行 id=1）。"""

    return [
        {
            "id": 1,
            "body": pb.expected_toast_body(),
            "qty": 12,
            "note": "added",
        }
    ]


class RealFixtureIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)
        self.result = build_outputs(FIXTURES / "changes.bin", CONTRACT, self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def test_build_is_clean_and_complete(self):
        self.assertTrue(self.result.ok)
        self.assertEqual(self.result.messages_consumed, 18)
        self.assertEqual(len(self.result.transactions), 4)
        self.assertEqual(len(self.result.records), 6)

    def test_full_field_against_independent_baseline(self):
        db = sqlite3.connect(self.out / "mirror.sqlite")
        db.row_factory = sqlite3.Row
        rows = [dict(r) for r in db.execute(
            "SELECT id, body, qty, note FROM records ORDER BY id"
        )]
        db.close()
        self.assertEqual(rows, expected_final_state())
        # 4096 字节 TOAST 内容逐字符相等（独立 md5 基准）。
        self.assertEqual(len(rows[0]["body"]), 4096)

    def test_state_after_every_commit_boundary(self):
        """每个 Commit 后的快照都与 source.sql 逐步推导结果一致。"""

        body = pb.expected_toast_body()
        expected_by_through = {
            4: [  # txn1 提交：两行初始插入
                {"id": 1, "body": body, "qty": 10},
                {"id": 2, "body": "small", "qty": 20},
            ],
            9: [  # txn2：qty 改动 + 主键 2->3
                {"id": 1, "body": body, "qty": 11},
                {"id": 3, "body": "small", "qty": 20},
            ],
            13: [  # txn3：追加 note 列，旧行新列为 NULL
                {"id": 1, "body": body, "qty": 12, "note": "added"},
                {"id": 3, "body": "small", "qty": 20, "note": None},
            ],
            17: expected_final_state(),  # txn4：删除 id=3
        }
        for through, expected in expected_by_through.items():
            report = inspect_at(FIXTURES / "changes.bin", CONTRACT, through)
            rows = report["snapshot"]["tables"]["records"]
            self.assertEqual(rows, expected, msg=f"boundary through={through}")

    def test_relation_resend_and_note_append_in_catalog(self):
        db = sqlite3.connect(self.out / "mirror.sqlite")
        db.row_factory = sqlite3.Row
        # 每个事务都重发 Relation，版本逐事务递增到 4。
        version = db.execute("SELECT version FROM _mirror_relations").fetchone()[0]
        self.assertEqual(version, 4)
        columns = json.loads(
            db.execute("SELECT columns_json FROM _mirror_relations").fetchone()[0]
        )
        self.assertEqual([c["name"] for c in columns], ["id", "body", "qty", "note"])
        self.assertEqual([c["type"] for c in columns],
                         ["int4", "text", "int4", "text"])
        # 物理 schema 确实扩展过：note 列存在且带 DEFAULT NULL。
        ddl = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='records'"
        ).fetchone()[0]
        self.assertIn('"note" TEXT DEFAULT NULL', ddl)
        db.close()

    def test_transactions_jsonl_metadata(self):
        txns = [json.loads(line) for line in
                (self.out / "transactions.jsonl").read_text().splitlines()]
        self.assertEqual([t["xid"] for t in txns], [731, 732, 733, 734])
        self.assertTrue(all(t["status"] == "committed" for t in txns))
        # LSN 同时保留无损整数与十六进制。
        first = txns[0]
        self.assertEqual(first["commit_lsn"], 22178400)
        self.assertEqual(first["commit_lsn_hex"], hex(22178400))
        self.assertEqual((first["message_from"], first["message_to"]), (0, 4))
        self.assertEqual((first["inserts"], first["updates"], first["deletes"]), (2, 0, 0))
        counts = [(t["inserts"], t["updates"], t["deletes"]) for t in txns]
        self.assertEqual(counts, [(2, 0, 0), (0, 2, 0), (0, 1, 0), (0, 0, 1)])

    def test_records_jsonl_full_fields(self):
        records = [json.loads(line) for line in
                   (self.out / "records.jsonl").read_text().splitlines()]
        by_index = {r["message_index"]: r for r in records}
        # TOAST 继承：msg#7 的 update 保留完整 4096 字节旧 body。
        update_toast = by_index[7]
        self.assertEqual(len(update_toast["new_row"]["body"]), 4096)
        self.assertEqual(update_toast["new_row"]["body"], pb.expected_toast_body())
        self.assertEqual(update_toast["row_before"]["qty"], 10)
        self.assertEqual(update_toast["new_row"]["qty"], 11)
        # 主键变更：K 定位旧键 2，新行键为 3，不残留 id=2。
        pk_change = by_index[8]
        self.assertEqual(pk_change["key"], {"id": 2})
        self.assertEqual(pk_change["new_row"]["id"], 3)
        # 删除留痕带删除前全字段。
        deletion = by_index[16]
        self.assertEqual(deletion["key"], {"id": 3})
        self.assertEqual(deletion["row_before"]["body"], "small")
        self.assertEqual(deletion["op"], "delete")

    def test_toast_survives_multiple_updates_unchanged(self):
        """4096 字节旧 TOAST 跨两次更新原样保留。"""

        db = sqlite3.connect(self.out / "mirror.sqlite")
        body = db.execute("SELECT body FROM records WHERE id=1").fetchone()[0]
        db.close()
        self.assertEqual(body, pb.expected_toast_body())


class MidTransactionVisibilityTests(unittest.TestCase):
    def test_begin_and_intermediate_messages_are_invisible(self):
        # Begin 之后、Commit 之前的任意边界都看不到该事务任何内容。
        for through in (0, 1, 2, 3):
            report = inspect_at(FIXTURES / "changes.bin", CONTRACT, through)
            self.assertEqual(report["committed_transactions"], [])
            self.assertNotIn("records", report["snapshot"]["tables"])
        # 停在事务中间时，open_transaction_xid 指向该未提交事务。
        mid = inspect_at(FIXTURES / "changes.bin", CONTRACT, 2)
        self.assertEqual(mid["open_transaction_xid"], 731)
        # 停在 Commit 边界时，没有打开的事务。
        committed = inspect_at(FIXTURES / "changes.bin", CONTRACT, 4)
        self.assertIsNone(committed["open_transaction_xid"])

    def test_second_txn_intermediate_changes_invisible(self):
        # msg#7/8 已更新但 msg#9 Commit 未到：镜像仍停留在 txn1。
        report = inspect_at(FIXTURES / "changes.bin", CONTRACT, 8)
        rows = report["snapshot"]["tables"]["records"]
        self.assertEqual({r["id"] for r in rows}, {1, 2})  # id=3 尚不可见
        self.assertEqual(rows[0]["qty"], 10)              # qty=11 尚不可见

    def test_schema_change_invisible_before_commit(self):
        # msg#11 Relation 已追加 note，但 msg#13 未提交：物理表无 note 列。
        report = inspect_at(FIXTURES / "changes.bin", CONTRACT, 12)
        rows = report["snapshot"]["tables"]["records"]
        self.assertTrue(all("note" not in r for r in rows))


class NegativeFixtureTests(unittest.TestCase):
    def test_unknown_relation_diagnostic_and_no_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = build_outputs(FIXTURES / "unknown-relation.bin", CONTRACT, out)
            self.assertFalse(result.ok)
            fatal = result.fatal
            self.assertEqual(fatal["error_type"], "UnknownRelationError")
            self.assertEqual(fatal["message_index"], 2)
            self.assertEqual(fatal["frame_offset"], 91)
            self.assertEqual(fatal["byte_offset"], 92)
            self.assertIn("4294967000", fatal["message"])
            # 未提交事务零泄漏：业务表根本没建，records.jsonl 为空。
            db = sqlite3.connect(out / "mirror.sqlite")
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM sqlite_master WHERE name='records'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                db.execute("SELECT count(*) FROM _mirror_transactions").fetchone()[0],
                0,
            )
            db.close()
            self.assertEqual((out / "records.jsonl").read_text(), "")
            txns = [json.loads(line) for line in
                    (out / "transactions.jsonl").read_text().splitlines()]
            self.assertEqual(txns[0]["status"], "aborted")
            self.assertEqual(txns[0]["xid"], 731)

    def test_truncated_diagnostic_keeps_committed_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            result = build_outputs(FIXTURES / "truncated.bin", CONTRACT, out)
            self.assertFalse(result.ok)
            fatal = result.fatal
            self.assertEqual(fatal["error_type"], "TruncatedFrameError")
            self.assertEqual(fatal["message_index"], 17)
            self.assertEqual(fatal["frame_offset"], 4772)
            self.assertEqual(fatal["byte_offset"], 4776)
            self.assertEqual(fatal["declared_length"], 26)
            self.assertEqual(fatal["available"], 23)
            # 已提交前缀保留：前三个事务在，第四个删除事务整体未发布。
            db = sqlite3.connect(out / "mirror.sqlite")
            db.row_factory = sqlite3.Row
            xids = [r[0] for r in db.execute(
                "SELECT xid FROM _mirror_transactions ORDER BY xid")]
            self.assertEqual(xids, [731, 732, 733])
            ids = [r[0] for r in db.execute("SELECT id FROM records ORDER BY id")]
            self.assertEqual(ids, [1, 3])  # id=3 的删除未生效
            db.close()
            txns = [json.loads(line) for line in
                    (out / "transactions.jsonl").read_text().splitlines()]
            self.assertEqual([t["status"] for t in txns],
                             ["committed", "committed", "committed", "aborted"])


class OutOfContextTests(unittest.TestCase):
    def test_empty_input_produces_clean_empty_mirror(self):
        result = run(b"", load_contract(CONTRACT), sqlite3.connect(":memory:"))
        self.assertTrue(result.ok)
        self.assertEqual(result.transactions, [])
        self.assertEqual(result.records, [])

    def test_dml_outside_transaction_rejected(self):
        data = pb.stream([
            pb.insert(1, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")])
        ])
        result = run(data, load_contract(CONTRACT), sqlite3.connect(":memory:"))
        self.assertEqual(result.fatal["error_type"], "MalformedMessageError")
        self.assertIn("outside of an open transaction", result.fatal["message"])

    def test_commit_without_begin_rejected(self):
        data = pb.stream([pb.commit(1, 2, 2)])
        result = run(data, load_contract(CONTRACT), sqlite3.connect(":memory:"))
        self.assertIn("outside of an open transaction", result.fatal["message"])

    def test_unknown_relid_inside_transaction_has_location(self):
        data = pb.stream([
            pb.begin(1, 1, 1),
            pb.insert(99, [pb.text_value("1"), pb.text_value("a"), pb.text_value("10")]),
            pb.commit(1, 2, 2),
        ])
        result = run(data, load_contract(CONTRACT), sqlite3.connect(":memory:"))
        self.assertEqual(result.fatal["error_type"], "UnknownRelationError")
        self.assertEqual(result.fatal["message_index"], 1)
        self.assertEqual(result.fatal["frame_offset"], 25)


class IncompleteAtEofTests(unittest.TestCase):
    def test_uncommitted_txn_marked_incomplete(self):
        # 取 changes.bin 前 14 条消息（txn4 的 Begin 之后、Commit 之前截断）。
        data = (FIXTURES / "changes.bin").read_bytes()
        frames = []
        offset = 0
        for _ in range(15):  # 0..14，停在 txn4 Begin(msg#14) 之后
            import struct as _struct
            (length,) = _struct.unpack(">I", data[offset:offset + 4])
            frames.append(data[offset:offset + 4 + length])
            offset += 4 + length
        partial = b"".join(frames)

        contract = load_contract(CONTRACT)
        db = sqlite3.connect(":memory:")
        result = run(partial, contract, db)
        db.close()
        self.assertTrue(result.ok)  # EOF 未提交不是致命协议错误
        statuses = [(t["xid"], t["status"]) for t in result.transactions]
        self.assertEqual(statuses, [(731, "committed"), (732, "committed"),
                                    (733, "committed"), (734, "incomplete")])
        incomplete = result.transactions[-1]
        self.assertEqual(incomplete["message_from"], 14)
        self.assertIsNone(incomplete["commit_lsn"])


if __name__ == "__main__":
    unittest.main()
