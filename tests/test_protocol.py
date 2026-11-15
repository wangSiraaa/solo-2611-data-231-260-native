"""分帧与 pgoutput 协议解析器的单元测试。

覆盖：真实 fixtures 的全消息解析、长度/标记越界、不支持特性的拒绝路径，
以及所有诊断都带消息序号与字节偏移。
"""

from __future__ import annotations

import struct
import unittest
from pathlib import Path

from pgmirror.errors import (
    FramingError,
    MalformedMessageError,
    TruncatedFrameError,
    UnsupportedFeatureError,
)
from pgmirror.framing import iter_frames
from pgmirror.protocol import (
    BeginMessage,
    CommitMessage,
    DeleteMessage,
    InsertMessage,
    RelationMessage,
    UpdateMessage,
    parse_message,
)

from tests import pgoutput_builder as pb

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class FramingTests(unittest.TestCase):
    def test_frames_real_file_with_offsets(self):
        data = (FIXTURES / "changes.bin").read_bytes()
        frames = list(iter_frames(data))
        self.assertEqual(len(frames), 18)
        # 帧偏移单调递增，且首尾恰好覆盖整个文件。
        self.assertEqual(frames[0].frame_offset, 0)
        for earlier, later in zip(frames, frames[1:]):
            self.assertEqual(
                later.frame_offset,
                earlier.frame_offset + 4 + len(earlier.payload),
            )
        self.assertEqual(
            frames[-1].frame_offset + 4 + len(frames[-1].payload),
            len(data),
        )

    def test_truncated_payload_reports_index_and_bytes(self):
        data = (FIXTURES / "truncated.bin").read_bytes()
        with self.assertRaises(TruncatedFrameError) as ctx:
            list(iter_frames(data))
        err = ctx.exception
        self.assertEqual(err.message_index, 17)
        self.assertEqual(err.frame_offset, 4772)
        self.assertEqual(err.byte_offset, 4776)
        self.assertEqual(err.declared_length, 26)
        self.assertEqual(err.available, 23)

    def test_truncated_length_header(self):
        with self.assertRaises(FramingError) as ctx:
            list(iter_frames(b"\x00\x01"))
        self.assertEqual(ctx.exception.message_index, 0)
        self.assertEqual(ctx.exception.byte_offset, 0)

    def test_zero_length_frame_rejected(self):
        with self.assertRaises(FramingError):
            list(iter_frames(struct.pack(">I", 0)))


class ProtocolTests(unittest.TestCase):
    def test_parse_all_real_messages(self):
        data = (FIXTURES / "changes.bin").read_bytes()
        parsed_types = []
        for frame in iter_frames(data):
            message_type, parsed = parse_message(
                frame.payload, frame.index, frame.frame_offset
            )
            parsed_types.append(message_type)
        self.assertEqual(
            parsed_types,
            ["B", "R", "I", "I", "C", "B", "R", "U", "U", "C",
             "B", "R", "U", "C", "B", "R", "D", "C"],
        )

    def test_begin_commit_fields_lossless(self):
        _, begin = parse_message(pb.begin(0x1526A60, 847989464149041, 731), 0, 0)
        self.assertIsInstance(begin, BeginMessage)
        self.assertEqual(begin.final_lsn, 0x1526A60)
        self.assertEqual(begin.xid, 731)

        _, commit = parse_message(pb.commit(0x1526A60, 0x1526A90, 197437), 4, 4252)
        self.assertIsInstance(commit, CommitMessage)
        self.assertEqual(commit.commit_lsn, 22178400)
        self.assertEqual(hex(commit.commit_lsn), "0x1526a60")

    def test_relation_layout_matches_real_bytes(self):
        message = pb.relation(
            16384,
            "public",
            "records",
            [(1, "id", 23, -1), (0, "body", 25, -1), (0, "qty", 23, -1)],
        )
        # 与 fixtures/changes.bin 第 1 条 Relation 的原字节逐字节一致。
        real = next(
            f for i, f in enumerate(iter_frames((FIXTURES / "changes.bin").read_bytes()))
            if i == 1
        )
        self.assertEqual(message, real.payload)

        _, parsed = parse_message(message, 1, 25)
        self.assertIsInstance(parsed, RelationMessage)
        self.assertEqual(parsed.relid, 16384)
        self.assertEqual(parsed.replica_identity, "d")
        self.assertEqual([c.name for c in parsed.columns], ["id", "body", "qty"])
        self.assertEqual([c.type_oid for c in parsed.columns], [23, 25, 23])
        self.assertTrue(parsed.columns[0].is_key)
        self.assertFalse(parsed.columns[1].is_key)

    def test_insert_tuple_markers(self):
        message = pb.insert(
            16384,
            [pb.text_value("1"), pb.text_value("x"), pb.text_value("10")],
        )
        _, parsed = parse_message(message, 2, 91)
        self.assertIsInstance(parsed, InsertMessage)
        self.assertEqual([v.marker for v in parsed.new_tuple], ["t", "t", "t"])
        self.assertEqual(parsed.new_tuple[0].raw, b"1")

    def test_update_with_and_without_k(self):
        without_k = pb.update(
            16384,
            [pb.text_value("1"), pb.unchanged_value(), pb.text_value("11")],
        )
        _, parsed = parse_message(without_k, 7, 4373)
        self.assertIsInstance(parsed, UpdateMessage)
        self.assertIsNone(parsed.old_key)
        self.assertEqual([v.marker for v in parsed.new_tuple], ["t", "u", "t"])

        with_k = pb.update(
            16384,
            [pb.text_value("3"), pb.text_value("small"), pb.text_value("20")],
            [pb.text_value("2"), pb.null_value(), pb.null_value()],
        )
        _, parsed_k = parse_message(with_k, 8, 4399)
        self.assertEqual([v.marker for v in parsed_k.old_key], ["t", "n", "n"])
        self.assertEqual(parsed_k.old_key[0].raw, b"2")

    def test_delete_is_key_tuple_only(self):
        message = pb.delete(
            16384,
            [pb.text_value("3"), pb.null_value(), pb.null_value(), pb.null_value()],
        )
        _, parsed = parse_message(message, 16, 4751)
        self.assertIsInstance(parsed, DeleteMessage)
        self.assertEqual(len(parsed.key_tuple), 4)
        self.assertEqual(parsed.key_tuple[0].raw, b"3")

    def test_trailing_bytes_detected(self):
        good = pb.begin(1, 2, 3)
        with self.assertRaises(MalformedMessageError) as ctx:
            parse_message(good + b"\x00", 0, 0)
        self.assertEqual(ctx.exception.message_index, 0)

    def test_truncated_message_body_detected(self):
        good = pb.relation(
            1, "public", "t", [(1, "id", 23, -1), (0, "body", 25, -1)]
        )
        with self.assertRaises(MalformedMessageError) as ctx:
            parse_message(good[:-3], 5, 100)
        self.assertEqual(ctx.exception.message_index, 5)
        self.assertEqual(ctx.exception.frame_offset, 100)

    def test_bad_tuple_marker_rejected(self):
        # 手工构造一个 marker 为 'x' 的 insert。
        payload = (
            b"I" + struct.pack(">I", 1) + b"N" + struct.pack(">H", 1) + b"x"
        )
        with self.assertRaises(MalformedMessageError):
            parse_message(payload, 3, 50)

    def test_unsupported_message_types(self):
        for marker, label in [(b"T", "TRUNCATE"), (b"Y", "streaming")]:
            with self.assertRaises(UnsupportedFeatureError) as ctx:
                parse_message(marker, 0, 0)
            self.assertIn(label.lower() if marker == b"T" else "streaming",
                          str(ctx.exception).lower())

    def test_binary_tuple_rejected(self):
        payload = b"I" + struct.pack(">I", 1) + b"b" + struct.pack(">H", 0)
        with self.assertRaises(UnsupportedFeatureError):
            parse_message(payload, 0, 0)

    def test_full_old_tuple_rejected(self):
        payload = b"D" + struct.pack(">I", 1) + b"O" + struct.pack(">H", 0)
        with self.assertRaises(UnsupportedFeatureError):
            parse_message(payload, 0, 0)

    def test_unknown_type_byte_rejected(self):
        with self.assertRaises(UnsupportedFeatureError) as ctx:
            parse_message(b"Z", 0, 0)
        self.assertEqual(ctx.exception.message_index, 0)
        self.assertEqual(ctx.exception.byte_offset, 0)


class IndependentEncoderCrossCheckTests(unittest.TestCase):
    """独立测试编码器重建的消息必须与真实导出字节逐字节相等。

    编码器不导入被测 parser，因此字节级一致说明解析器理解的布局就是
    PostgreSQL 16 实际写出的布局，而不是两套代码共享同一个错误假设。
    """

    def setUp(self):
        self.data = (FIXTURES / "changes.bin").read_bytes()

    def _real(self, target_index):
        offset = 0
        index = 0
        while True:
            (length,) = struct.unpack(">I", self.data[offset:offset + 4])
            if index == target_index:
                return self.data[offset + 4:offset + 4 + length]
            offset += 4 + length
            index += 1

    def test_all_message_types_byte_identical(self):
        cases = {
            0: pb.begin(0x1526A60, 847989464149041, 731),
            1: pb.relation(
                16384, "public", "records",
                [(1, "id", 23, -1), (0, "body", 25, -1), (0, "qty", 23, -1)],
            ),
            11: pb.relation(
                16384, "public", "records",
                [(1, "id", 23, -1), (0, "body", 25, -1),
                 (0, "qty", 23, -1), (0, "note", 25, -1)],
            ),
            16: pb.delete(
                16384,
                [pb.text_value("3"), pb.null_value(), pb.null_value(), pb.null_value()],
            ),
        }
        for index, built in cases.items():
            self.assertEqual(built, self._real(index), msg=f"message #{index}")

        # Commit 的时间戳是完整微秒值，逐字段解析后重建。
        for index in (4, 9, 13, 17):
            real = self._real(index)
            flags = real[1]
            lc, end, ts = struct.unpack(">QQQ", real[2:26])
            self.assertEqual(pb.commit(lc, end, ts, flags), real)


if __name__ == "__main__":
    unittest.main()
