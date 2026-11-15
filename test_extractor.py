#!/usr/bin/env python3
"""extractor.py 的自动测试。

核心抽取（extractor.py）不依赖 sqlite3；本测试文件把 sqlite3 仅用作
独立验证 oracle：对 fixtures/source.sqlite 完整比对所有行与 BLOB 字节。

运行：python3 -m unittest -v
"""
from __future__ import annotations

import json
import re
import sqlite3
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import extractor  # noqa: E402
from extractor import CorruptError  # noqa: E402

DB = ROOT / "fixtures" / "source.sqlite"
TRUNCATED_DB = ROOT / "fixtures" / "truncated.sqlite"
TABLE_JSON = ROOT / "table.json"
BAD_TABLE_JSON = ROOT / "bad-table.json"
COLUMNS = ["id", "label", "amount", "payload", "note"]


def oracle_rows():
    """用 sqlite3 独立查询全部行；BLOB 取 hex 以便逐字节比较。"""
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    try:
        sql = ("SELECT id, label, amount, "
               "CASE WHEN payload IS NULL THEN NULL ELSE lower(hex(payload)) END, "
               "note FROM records ORDER BY id")
        return [dict(zip(COLUMNS, row)) for row in con.execute(sql)]
    finally:
        con.close()


class VarintTest(unittest.TestCase):
    def test_one_byte(self):
        self.assertEqual(extractor.parse_varint(b"\x00", 0, 1, "t"), (0, 1))
        self.assertEqual(extractor.parse_varint(b"\x7f", 0, 1, "t"), (127, 1))

    def test_multi_byte(self):
        self.assertEqual(extractor.parse_varint(b"\x81\x00", 0, 2, "t"), (128, 2))
        # 16384 -> 0x81 0x80 0x00（3 字节）
        self.assertEqual(extractor.parse_varint(b"\x81\x80\x00", 0, 3, "t"), (16384, 3))

    def test_eight_byte(self):
        self.assertEqual(
            extractor.parse_varint(b"\xff" * 7 + b"\x7f", 0, 8, "t"),
            ((1 << 56) - 1, 8),
        )

    def test_nine_byte_negative_rowid(self):
        # 负 rowid 必为 9 字节 varint：0xFF*8 + 0x7E == 2**64 - 130
        value, used = extractor.parse_varint(b"\xff" * 8 + b"\x7e", 0, 9, "t")
        self.assertEqual(used, 9)
        self.assertEqual(value, (1 << 64) - 130)
        self.assertEqual(extractor.to_int64(value), -130)

    def test_nine_byte_min_int64(self):
        # 0xC0 0x80*7 0x00 -> 0x8000000000000000 == -2**63
        value, used = extractor.parse_varint(b"\xc0" + b"\x80" * 7 + b"\x00", 0, 9, "t")
        self.assertEqual(used, 9)
        self.assertEqual(extractor.to_int64(value), -(1 << 63))

    def test_truncated_raises(self):
        with self.assertRaises(CorruptError):
            extractor.parse_varint(b"\x80", 0, 1, "t")  # 续位置位但没有后续字节

    def test_to_int64(self):
        self.assertEqual(extractor.to_int64(0), 0)
        self.assertEqual(extractor.to_int64((1 << 63) - 1), (1 << 63) - 1)
        self.assertEqual(extractor.to_int64(1 << 63), -(1 << 63))
        self.assertEqual(extractor.to_int64((1 << 64) - 1), -1)


class LocalPayloadFormulaTest(unittest.TestCase):
    """表叶 cell 页内载荷公式（usableSize=512 时的具体数值）。"""

    def test_thresholds(self):
        # maxLocal = 512 - 35 = 477；minLocal = (500*32)//255 - 23 = 39
        self.assertEqual(extractor.local_payload_size(0, 512), 0)
        self.assertEqual(extractor.local_payload_size(477, 512), 477)   # 恰好全本地
        self.assertEqual(extractor.local_payload_size(100, 512), 100)

    def test_overflow_spill(self):
        # P=478：K = 39 + (478-39) % 508 = 478 > 477 -> 回退 minLocal=39
        self.assertEqual(extractor.local_payload_size(478, 512), 39)
        # P=1514（fixture 中 1500 字节 BLOB 行）：K = 39 + 1475 % 508 = 498 > 477 -> 39
        self.assertEqual(extractor.local_payload_size(1514, 512), 39)
        # 构造一个 K <= maxLocal 的情形：P=520 -> K = 39 + 481 % 508 = 520? 否：
        # K = 39 + (520-39) % 508 = 39 + 481 = 520 > 477 -> 39；P=530 -> K=39+491=530>477
        # P=1000 -> K = 39 + 961 % 508 = 39 + 453 = 492 > 477 -> 39
        # P=476+508=984 -> K = 39 + 945 % 508 = 39 + 437 = 476 <= 477 -> 476
        self.assertEqual(extractor.local_payload_size(984, 512), 476)

    def test_other_page_sizes(self):
        # usableSize=4096：maxLocal=4061，minLocal=(4084*32)//255-23=489
        self.assertEqual(extractor.local_payload_size(4061, 4096), 4061)
        self.assertEqual(extractor.local_payload_size(4062, 4096), 489)


class RecordParseTest(unittest.TestCase):
    def _payload(self):
        serials = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12 + 2 * 3, 13 + 2 * 2]
        header = bytes([1 + len(serials)] + serials)
        body = b"".join([
            b"",                                  # NULL
            b"\xfb",                              # int8 = -5
            b"\x12\x34",                          # int16 = 4660
            b"\xff\xff\xff",                      # int24 = -1
            b"\x01\x02\x03\x04",                  # int32
            b"\xff\xff\xff\xff\xff\xfe",          # int48 = -2
            b"\x01\x02\x03\x04\x05\x06\x07\x08",  # int64
            struct.pack(">d", 1.5),               # float64
            b"", b"",                             # 常量 0 / 1
            b"\x00\xab\xff",                      # BLOB(3)
            "é".encode("utf-8"),                  # TEXT(2)
        ])
        return header + body

    def test_all_serial_types(self):
        cols = [f"c{i}" for i in range(12)]
        values = extractor.parse_record(self._payload(), cols, None, 99, "t")
        self.assertEqual(
            list(values.values()),
            [None, -5, 0x1234, -1, 0x01020304, -2, 0x0102030405060708,
             1.5, 0, 1, b"\x00\xab\xff", "é"],
        )

    def test_integer_primary_key_from_rowid(self):
        # IPK 列在 record 中为 NULL，逻辑值必须取自 rowid
        payload = bytes([3, 0, 13])  # header=3 字节，2 列：NULL, TEXT(0)
        values = extractor.parse_record(payload, ["id", "name"], 0, -130, "t")
        self.assertEqual(values, {"id": -130, "name": ""})

    def test_missing_trailing_columns_default_null(self):
        payload = bytes([3, 0, 1]) + b"\x2a"  # 2 列（NULL, int8=42），表有 3 列
        values = extractor.parse_record(payload, ["a", "b", "c"], None, 1, "t")
        self.assertEqual(values, {"a": None, "b": 42, "c": None})

    def test_reserved_serial_type_rejected(self):
        with self.assertRaises(CorruptError):
            extractor.parse_record(bytes([2, 0, 10]), ["a", "b"], None, 1, "t")

    def test_header_body_mismatch_rejected(self):
        # serial type 6 需要 8 字节，体里只有 2 字节
        with self.assertRaises(CorruptError):
            extractor.parse_record(bytes([2, 0, 6]) + b"\x01\x02", ["a", "b"], None, 1, "t")
        # 体里多出 1 字节未消费
        with self.assertRaises(CorruptError):
            extractor.parse_record(bytes([2, 0, 1]) + b"\x01\x02", ["a", "b"], None, 1, "t")

    def test_header_len_beyond_payload_rejected(self):
        with self.assertRaises(CorruptError):
            extractor.parse_record(bytes([9, 0]), ["a"], None, 1, "t")

    def test_too_many_columns_rejected(self):
        with self.assertRaises(CorruptError):
            extractor.parse_record(bytes([3, 0, 0, 0]), ["a"], None, 1, "t")


class HeaderValidationTest(unittest.TestCase):
    def test_fixture_header(self):
        header = extractor.parse_db_header(DB.read_bytes())
        self.assertEqual(header.page_size, 512)
        self.assertEqual(header.reserved, 0)
        self.assertEqual(header.usable_size, 512)
        self.assertEqual(header.page_count, 12)
        self.assertEqual(header.encoding, 1)

    def test_bad_magic(self):
        data = bytearray(DB.read_bytes())
        data[0] = ord("X")
        with self.assertRaisesRegex(CorruptError, "魔数"):
            extractor.parse_db_header(bytes(data))

    def test_wal_rejected(self):
        data = bytearray(DB.read_bytes())
        data[18] = 2
        with self.assertRaisesRegex(CorruptError, "WAL"):
            extractor.parse_db_header(bytes(data))

    def test_utf16_rejected(self):
        data = bytearray(DB.read_bytes())
        struct.pack_into(">I", data, 56, 2)
        with self.assertRaisesRegex(CorruptError, "UTF-8"):
            extractor.parse_db_header(bytes(data))

    def test_truncated_file_rejected(self):
        with self.assertRaisesRegex(CorruptError, "截断|整数倍"):
            extractor.extract_records(TRUNCATED_DB, TABLE_JSON)

    def test_rootpage_out_of_range(self):
        with self.assertRaisesRegex(CorruptError, "rootpage 99999 越界.*12 页"):
            extractor.extract_records(DB, BAD_TABLE_JSON)

    def test_wrong_page_type_rejected(self):
        # 把 rootpage 指到 overflow 页（第 3 页），应报页类型不支持
        spec = json.loads(Path(TABLE_JSON).read_text())
        spec["rootpage"] = 3
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(spec, f)
            tmp = f.name
        try:
            with self.assertRaisesRegex(CorruptError, "页类型"):
                extractor.extract_records(DB, tmp)
        finally:
            Path(tmp).unlink()


class FullExtractionTest(unittest.TestCase):
    """整表抽取，并与 sqlite3 独立查询结果完整比对。"""

    @classmethod
    def setUpClass(cls):
        cls.rows = extractor.extract_records(DB, TABLE_JSON)
        cls.oracle = oracle_rows()

    def test_row_count_and_order(self):
        self.assertEqual(len(self.rows), 87)
        rowids = [r["rowid"] for r in self.rows]
        self.assertEqual(rowids, sorted(rowids), "输出必须按 rowid 升序")
        self.assertEqual(len(set(rowids)), len(rowids), "rowid 不得重复")

    def test_all_rows_match_sqlite_oracle(self):
        self.assertEqual(len(self.rows), len(self.oracle))
        for got, expected in zip(self.rows, self.oracle):
            self.assertEqual(got["table"], "records")
            self.assertEqual(got["rowid"], expected["id"])
            self.assertEqual(
                got["values"], expected,
                f"rowid {expected['id']} 的逻辑值与 sqlite3 查询结果不一致",
            )
            self.assertEqual(got["source"]["rowid"], expected["id"])

    def test_blob_bytes_match_exactly(self):
        # 显式逐字节复核 BLOB：oracle 的 lower(hex(payload)) 与输出 hex 串相等
        for got, expected in zip(self.rows, self.oracle):
            self.assertEqual(got["values"]["payload"], expected["payload"])
        big = next(r for r in self.rows if r["rowid"] == 128)
        self.assertEqual(len(big["values"]["payload"]), 1500 * 2)  # hex 长度翻倍
        self.assertRegex(big["values"]["payload"], r"^[0-9a-f]+$")

    def test_source_fields_complete(self):
        for r in self.rows:
            src = r["source"]
            self.assertEqual(set(src), {"leaf_page", "cell_index", "rowid",
                                        "payload_bytes", "overflow_pages"})
            self.assertGreaterEqual(src["leaf_page"], 1)
            self.assertGreaterEqual(src["cell_index"], 0)
            self.assertGreater(src["payload_bytes"], 0)
            self.assertIsInstance(src["overflow_pages"], list)

    def test_fixture_really_uses_interior_and_overflow_pages(self):
        """确认材料确实触发内部页与 overflow 页，而非只在空表/单页上通过。"""
        data = DB.read_bytes()
        self.assertEqual(data[512], 0x05, "第 2 页（rootpage）必须是内部表页 0x05")
        leaves = {r["source"]["leaf_page"] for r in self.rows}
        self.assertGreaterEqual(len(leaves), 2, "必须跨多个叶页")
        big = next(r for r in self.rows if r["rowid"] == 128)
        self.assertEqual(len(big["source"]["overflow_pages"]), 3,
                         "1500 字节 BLOB 必须跨 3 个 overflow 页")
        self.assertEqual(big["source"]["payload_bytes"], 1514)
        for r in self.rows:
            if r["rowid"] != 128:
                self.assertEqual(r["source"]["overflow_pages"], [])

    def test_edge_values(self):
        by_id = {r["rowid"]: r["values"] for r in self.rows}
        # 负 rowid（存储为 9 字节 varint）与 0
        self.assertIn(-130, by_id)
        self.assertIn(-1, by_id)
        self.assertIn(0, by_id)
        self.assertEqual(by_id[-130]["id"], -130)   # IPK 逻辑值来自 rowid
        # 大 rowid（3 字节 varint）
        self.assertIn(16384, by_id)
        # 6 字节有符号整数（serial type 5）
        self.assertEqual(by_id[-130]["amount"], -1099511627906)
        # Unicode（含非 BMP 字符）
        self.assertEqual(by_id[127]["label"], "中文𝄞-127")
        # NULL / 空串 / 空 BLOB
        self.assertIsNone(by_id[0]["amount"])
        self.assertEqual(by_id[0]["payload"], "")   # 空 BLOB -> 空 hex 串
        self.assertEqual(by_id[0]["note"], "")      # 空字符串
        self.assertIsNone(by_id[1]["note"])

    def test_published_records_jsonl_matches(self):
        """已发布的 records.jsonl（若存在）必须与重新抽取一致。"""
        published = ROOT / "records.jsonl"
        if not published.exists():
            self.skipTest("records.jsonl 尚未生成")
        lines = published.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), len(self.oracle))
        for line, expected in zip(lines, self.oracle):
            self.assertEqual(json.loads(line)["values"], expected)


class CliTest(unittest.TestCase):
    def test_cli_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.jsonl"
            proc = subprocess.run(
                [sys.executable, str(ROOT / "extractor.py"),
                 str(DB), str(TABLE_JSON), "-o", str(out)],
                capture_output=True, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("87 行", proc.stderr)
            lines = out.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 87)
            oracle = oracle_rows()
            for line, expected in zip(lines, oracle):
                self.assertEqual(json.loads(line)["values"], expected)

    def test_cli_truncated_input_fails_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "extractor.py"),
                 str(TRUNCATED_DB), str(TABLE_JSON), "-o", str(Path(tmp) / "x.jsonl")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("截断", proc.stderr)

    def test_cli_bad_rootpage_fails_with_diagnostic(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, str(ROOT / "extractor.py"),
                 str(DB), str(BAD_TABLE_JSON), "-o", str(Path(tmp) / "x.jsonl")],
                capture_output=True, text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("rootpage 99999", proc.stderr)


class CorePurityTest(unittest.TestCase):
    def test_core_does_not_import_sqlite3(self):
        src = (ROOT / "extractor.py").read_text(encoding="utf-8")
        self.assertIsNone(
            re.search(r"^\s*(import|from)\s+sqlite3", src, re.MULTILINE),
            "核心抽取器不得 import sqlite3",
        )


if __name__ == "__main__":
    unittest.main()
