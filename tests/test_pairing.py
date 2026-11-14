"""端到端测试与独立重读校验。

覆盖需求列出的全部样例类型：乱序、wrapped 序列、以 @/+ 开头的质量行、
同名重复、角色冲突、孤儿、坏尾、gzip 偏移、UTF-8 字节序、小窗口落盘。

独立校验器 ``IndependentVerifier`` 不导入 fastqpair.parser，
而是用自己的字节范围切片逻辑重读来源原件，逐配对核对序列/质量，
而不是只比较配对总数。
"""

from __future__ import annotations

import base64
import gzip
import json
import os
import shutil
import tempfile
import unittest

from fastqpair.engine import (
    AMBIGUOUS_NAME,
    ORPHAN_NAME,
    PAIRED_R1_NAME,
    PAIRED_R2_NAME,
    QUARANTINE_NAME,
    REPORT_NAME,
    run_pairing,
)
from fastqpair.model import Config, SourceSpec


# --------------------------------------------------------------------------
# 独立校验器：与生产代码完全独立的一套只读核对逻辑
# --------------------------------------------------------------------------
class IndependentVerifier:
    """按 manifest 的字节范围重读来源，独立重建记录并核对产物。"""

    def __init__(self, workdir: str):
        self.workdir = workdir
        self._plain_cache: dict[str, bytes] = {}
        self._gz_cache: dict[str, bytes] = {}

    def _source_bytes(self, path: str, compression: str) -> bytes:
        if compression == "gzip":
            if path not in self._gz_cache:
                with gzip.open(path, "rb") as fh:
                    self._gz_cache[path] = fh.read()
            return self._gz_cache[path]
        if path not in self._plain_cache:
            with open(path, "rb") as fh:
                self._plain_cache[path] = fh.read()
        return self._plain_cache[path]

    @staticmethod
    def _b64(data: str | None):
        return None if data is None else base64.b64decode(data)

    def extract_record(self, entry: dict) -> tuple[bytes, bytes, bytes]:
        """按清单字节范围切出原始四行块，独立解析出头/序列/质量。"""
        src = entry
        rng = src["byte_range"]
        blob = self._source_bytes(src["source_path"], src["compression"])
        chunk = blob[rng["start"]:rng["end"]]
        assert chunk.startswith(b"@"), "字节范围未从头行开始"
        lines = chunk.splitlines()
        assert len(lines) >= 4 and lines[0] == self._b64(src["raw_header_b64"])
        # 独立的多行包装解析：+ 行之前全是序列，之后按序列长度累计质量。
        plus_idx = next(i for i, line in enumerate(lines) if line.startswith(b"+"))
        seq = b"".join(lines[1:plus_idx])
        qual_parts, qual_len = [], 0
        j = plus_idx + 1
        while qual_len < len(seq):
            assert j < len(lines), "独立解析：质量不足"
            qual_parts.append(lines[j])
            qual_len += len(lines[j])
            j += 1
        assert qual_len == len(seq), "独立解析：质量超长"
        return lines[0], seq, b"".join(qual_parts)

    def verify_run(self) -> dict:
        manifest_path = os.path.join(self.workdir, "manifest.json")
        with open(manifest_path, encoding="utf-8") as fh:
            manifest = json.load(fh)
        report_path = os.path.join(self.workdir, REPORT_NAME)
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)

        # 1) 清单内每条记录的字节范围都能独立切回来源并重建头/序列/质量。
        for entry in manifest["records"]:
            header, seq, qual = self.extract_record(entry)
            assert header == self._b64(entry["raw_header_b64"])
            assert len(seq) == len(qual)

        if not report["complete"]:
            # 不完整运行不得发布配对 FASTQ。
            assert not os.path.exists(os.path.join(self.workdir, PAIRED_R1_NAME))
            assert not os.path.exists(os.path.join(self.workdir, PAIRED_R2_NAME))
            return report

        # 2) 完整运行：逐配对核对两份 FASTQ 与来源原件。
        r1 = self._read_fastq(PAIRED_R1_NAME)
        r2 = self._read_fastq(PAIRED_R2_NAME)
        assert len(r1) == len(r2)
        # 两份文件行数严格一致（每条 4 行）、read_id 顺序一致（UTF-8 字节序）。
        ids = [self._id_of(h) for h, _, _ in r1]
        assert ids == sorted(ids, key=lambda b: b)
        assert [self._id_of(h) for h, _, _ in r2] == ids

        entries_by_key: dict[tuple[bytes, int], dict] = {}
        for entry in manifest["records"]:
            if entry["status"] == "paired":
                entries_by_key[(self._b64(entry["read_id_b64"]), entry["mate"])] = entry

        for (h1, s1, q1), (h2, s2, q2) in zip(r1, r2):
            rid = self._id_of(h1)
            assert self._id_of(h2) == rid
            e1, e2 = entries_by_key[(rid, 1)], entries_by_key[(rid, 2)]
            # 输出与来源逐字节核对（经独立切片）。
            oh1, os1, oq1 = self.extract_record(e1)
            oh2, os2, oq2 = self.extract_record(e2)
            assert h1 == oh1 and s1 == os1 and q1 == oq1
            assert h2 == oh2 and s2 == os2 and q2 == oq2
            assert len(s1) == len(q1) and len(s2) == len(q2)
        return report

    def _read_fastq(self, name: str) -> list[tuple[bytes, bytes, bytes]]:
        with open(os.path.join(self.workdir, name), "rb") as fh:
            data = fh.read()
        lines = data.split(b"\n")
        assert lines[-1] == b"", "FASTQ 必须以换行结束"
        lines = lines[:-1]
        assert len(lines) % 4 == 0, "必须是规范四行 FASTQ"
        out = []
        for i in range(0, len(lines), 4):
            assert lines[i].startswith(b"@")
            assert lines[i + 2] == b"+"
            out.append((lines[i], lines[i + 1], lines[i + 3]))
        return out

    @staticmethod
    def _id_of(header: bytes) -> bytes:
        token = header[1:].split()[0]
        if token.endswith(b"/1") or token.endswith(b"/2"):
            token = token[:-2]
        return token


# --------------------------------------------------------------------------
# 样例构造辅助
# --------------------------------------------------------------------------
def rec(header: str, seq: str | list[str], qual: str | list[str]) -> bytes:
    seq_lines = seq if isinstance(seq, list) else [seq]
    qual_lines = qual if isinstance(qual, list) else [qual]
    out = header.encode() + b"\n"
    out += b"".join(s.encode() + b"\n" for s in seq_lines)
    out += b"+\n"
    out += b"".join(q.encode() + b"\n" for q in qual_lines)
    return out


def write_file(path: str, chunks: list[bytes], gz: bool = False) -> None:
    data = b"".join(chunks)
    if gz:
        with gzip.open(path, "wb") as fh:
            fh.write(data)
    else:
        with open(path, "wb") as fh:
            fh.write(data)


class PairingTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.out = os.path.join(self.tmp, "out")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _src(self, name: str, mate: int, compression: str = "auto") -> SourceSpec:
        return SourceSpec(os.path.join(self.tmp, name), mate, compression)

    def run_pair(self, sources, window=2, schemes=("suffix", "casava")):
        cfg = Config(pending_record_limit=window, identity_schemes=schemes)
        return run_pairing(sources, cfg, self.out)


class TestHappyPathAndFixtures(PairingTestBase):
    def test_fixtures_full_classification_and_independent_reread(self):
        r1 = os.path.join(self.tmp, "R1.fastq")
        r2gz = os.path.join(self.tmp, "R2.fastq.gz")
        shutil.copy("/workspace/fixtures/R1.fastq", r1)
        shutil.copy("/workspace/fixtures/R2.fastq.gz", r2gz)

        result = self.run_pair([self._src("R1.fastq", 1), self._src("R2.fastq.gz", 2)])

        self.assertTrue(result.complete)
        statuses = {g.read_id.decode(): g.status for g in result.groups}
        self.assertEqual(statuses["readA"], "paired")
        self.assertEqual(statuses["readB"], "paired")
        self.assertEqual(statuses["dup"], "ambiguous")
        self.assertEqual(statuses["orphan"], "orphan")          # 单侧缺失
        self.assertEqual(statuses["onlyR2"], "orphan")
        self.assertEqual(result.paired_count, 2)
        # 窗口=2，而合法记录有 9 条：必须发生落盘且内存峰值不超过窗口。
        self.assertGreater(result.spill_count, 0)
        self.assertLessEqual(result.max_memory_records, 2)

        # 角色冲突被隔离。
        self.assertEqual(len(result.quarantine), 1)
        self.assertIn("identity", result.quarantine[0].reason)

        report = IndependentVerifier(self.out).verify_run()
        # gzip 来源必须带 compression=gzip 与压缩原件 SHA256，偏移空间是解压流。
        gz_src = next(s for s in report["sources"] if s["compression"] == "gzip")
        self.assertEqual(len(gz_src["file_sha256"]), 64)
        with open(r2gz, "rb") as fh:
            import hashlib
            expect_sha = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(gz_src["file_sha256"], expect_sha)
        with open(os.path.join(self.out, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        gz_entries = [e for e in manifest["records"]
                      if e["source_path"].endswith("R2.fastq.gz")]
        self.assertTrue(gz_entries)
        for e in gz_entries:
            self.assertEqual(e["byte_range"]["space"], "decompressed_fastq_stream")
            # gzip 偏移不得冒充物理字节范围：解压流长度远小于压缩文件时，
            # 偏移仍应落在解压流长度之内。
            with gzip.open(r2gz, "rb") as fh:
                stream_len = len(fh.read())
            self.assertLessEqual(e["byte_range"]["end"], stream_len)

        # 两份 FASTQ 行数严格一致且同为 4 的倍数。
        for name in (PAIRED_R1_NAME, PAIRED_R2_NAME):
            with open(os.path.join(self.out, name), "rb") as fh:
                self.assertEqual(len(fh.read().splitlines()), 4 * result.paired_count)

    def test_wrapped_seq_and_quality_starting_with_at_or_plus(self):
        # 质量行以 @ 和 + 开头，绝不能被误判为下一记录头/分隔行。
        r1 = [
            rec("@w/1", ["ACGT", "NN"], ["@@+", "II!"]),  # 4+2=6，质量 3+3=6
        ]
        r2 = [rec("@w/2", "NNACGT", "+++@@@")]
        write_file(os.path.join(self.tmp, "a.fq"), r1)
        write_file(os.path.join(self.tmp, "b.fq"), r2)
        result = self.run_pair([self._src("a.fq", 1), self._src("b.fq", 2)])
        self.assertEqual(result.paired_count, 1)
        grp = result.groups[0]
        self.assertEqual(grp.mates[1][0].seq, b"ACGTNN")
        self.assertEqual(grp.mates[1][0].qual, b"@@+II!")
        IndependentVerifier(self.out).verify_run()

    def test_out_of_order_mates_pair_after_full_scan(self):
        # R2 的身份顺序与 R1 完全相反。
        r1 = [rec(f"@g{i}/1", "AC", "II") for i in range(5)]
        r2 = [rec(f"@g{i}/2", "GT", "JJ") for i in range(4, -1, -1)]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq"), r2)
        result = self.run_pair([self._src("r1.fq", 1), self._src("r2.fq", 2)], window=1)
        self.assertEqual(result.paired_count, 5)
        self.assertGreater(result.spill_count, 0)
        self.assertLessEqual(result.max_memory_records, 1)
        IndependentVerifier(self.out).verify_run()

    def test_quality_line_looking_like_header_does_not_split_record(self):
        # 质量行恰好以 @ 开头且内容像头；其后紧跟真正的下一记录。
        r1 = [
            rec("@q1/1", "AC", "@I"),
            rec("@q2/1", "AC", "II"),
        ]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        result = self.run_pair([self._src("r1.fq", 1)])
        self.assertEqual([g.read_id for g in result.orphans], [b"q1", b"q2"])
        self.assertEqual(result.quarantine, [])
        # 字节范围必须覆盖完整两条记录且首尾相接。
        with open(os.path.join(self.out, "manifest.json"), encoding="utf-8") as fh:
            manifest = json.load(fh)
        ranges = sorted(
            (e["byte_range"]["start"], e["byte_range"]["end"])
            for e in manifest["records"]
        )
        self.assertEqual(ranges, [(0, 14), (14, 28)])

    def test_late_duplicate_overturns_tentative_pair_only_after_full_scan(self):
        # 前半段 k 已构成看似完整的配对；重复身份在很后面才出现，
        # 最终整组必须是 ambiguous，不能曾发布成配对。
        r1 = [rec("@k/1", "AC", "II")] + [rec(f"@f{i}/1", "A", "I") for i in range(6)]
        r1.append(rec("@k/1", "GT", "JJ"))
        r2 = [rec("@k/2", "GG", "HH")] + [rec(f"@f{i}/2", "T", "!") for i in range(6)]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq"), r2)
        result = self.run_pair([self._src("r1.fq", 1), self._src("r2.fq", 2)], window=1)
        k = next(g for g in result.groups if g.read_id == b"k")
        self.assertEqual(k.status, "ambiguous")
        self.assertEqual(result.paired_count, 6)
        # 配对产物中绝不能出现 k。
        with open(os.path.join(self.out, PAIRED_R1_NAME), "rb") as fh:
            self.assertNotIn(b"@k/1", fh.read())
        IndependentVerifier(self.out).verify_run()


class TestDuplicatesAndIdentity(PairingTestBase):
    def test_duplicate_mate_marks_whole_group_ambiguous(self):
        r1 = [rec("@d/1", "AC", "II"), rec("@d/1", "GT", "JJ")]
        r2 = [rec("@d/2", "GG", "HH")]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq"), r2)
        result = self.run_pair([self._src("r1.fq", 1), self._src("r2.fq", 2)])
        self.assertEqual(result.paired_count, 0)
        grp = result.groups[0]
        self.assertEqual(grp.status, "ambiguous")
        self.assertEqual(len(grp.mates[1]), 2)
        with open(os.path.join(self.out, AMBIGUOUS_NAME), encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual(len(payload[0]["records"]), 3)

    def test_duplicate_on_both_mates_no_many_to_many(self):
        r1 = [rec("@d/1", "A", "I"), rec("@d/1", "C", "J")]
        r2 = [rec("@d/2", "G", "H"), rec("@d/2", "T", "!")]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq"), r2)
        result = self.run_pair([self._src("r1.fq", 1), self._src("r2.fq", 2)])
        self.assertEqual(result.groups[0].status, "ambiguous")
        self.assertEqual(result.paired_count, 0)

    def test_role_mismatch_and_conflict_and_empty_identity_quarantined(self):
        r1 = [
            rec("@x/2", "AC", "II"),                       # 后缀 mate2 与角色 1 不符
            rec("@y/1 2:N:0:X", "AC", "II"),               # 声明冲突
            rec("@", "AC", "II"),                          # 空身份
            rec("@ok/1", "AC", "II"),
        ]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        result = self.run_pair([self._src("r1.fq", 1)])
        reasons = " ".join(q.reason for q in result.quarantine)
        self.assertIn("角色不符", reasons)
        self.assertIn("声明冲突", reasons)
        self.assertIn("空身份", reasons)
        self.assertEqual(len(result.quarantine), 3)
        # ok 为孤儿（无 R2），且不能与任何序列相同者合并身份。
        self.assertEqual([g.read_id for g in result.orphans], [b"ok"])

    def test_identical_sequence_does_not_merge_identity(self):
        r1 = [rec("@alpha/1", "ACGT", "IIII"), rec("@beta/1", "ACGT", "IIII")]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        result = self.run_pair([self._src("r1.fq", 1)])
        self.assertEqual({g.read_id for g in result.orphans}, {b"alpha", b"beta"})

    def test_casava_default_mate_without_declaration(self):
        # 无任何 mate 声明时使用文件角色；名称取第一 token。
        r1 = [rec("@plainName description", "AC", "II")]
        r2 = [rec("@plainName extra", "GT", "JJ")]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq"), r2)
        result = self.run_pair([self._src("r1.fq", 1), self._src("r2.fq", 2)])
        self.assertEqual(result.paired_count, 1)

    def test_identity_scheme_switch_casava_only_and_suffix_only(self):
        # 仅启用 casava：/1 后缀不再是声明，mate 完全由角色缺省决定，
        # 名称保留 /1 后缀（不剥离），因此两个文件名不同 -> 孤儿。
        write_file(os.path.join(self.tmp, "r1.fq"), [rec("@s/1", "AC", "II")])
        write_file(os.path.join(self.tmp, "r2.fq"), [rec("@s/2", "GT", "JJ")])
        cfg = Config(pending_record_limit=2, identity_schemes=("casava",))
        result = run_pairing(
            [self._src("r1.fq", 1), self._src("r2.fq", 2)], cfg, self.out
        )
        self.assertEqual({g.read_id for g in result.orphans}, {b"s/1", b"s/2"})

        # 仅启用 suffix：CASAVA 第二字段被忽略，后缀声明与角色一致 -> 配对。
        shutil.rmtree(self.out, ignore_errors=True)
        write_file(os.path.join(self.tmp, "c1.fq"), [rec("@c/1 1:N:0:X", "AC", "II")])
        write_file(os.path.join(self.tmp, "c2.fq"), [rec("@c/2 2:N:0:X", "GT", "JJ")])
        cfg2 = Config(pending_record_limit=2, identity_schemes=("suffix",))
        result2 = run_pairing(
            [self._src("c1.fq", 1), self._src("c2.fq", 2)], cfg2, self.out
        )
        self.assertEqual(result2.paired_count, 1)
        self.assertEqual(result2.paired_ids, [b"c"])

    def test_config_from_descriptive_fixture_dict(self):
        # fixtures/config.json 是描述性键名，加载器应推断出窗口与两套身份方案。
        with open("/workspace/fixtures/config.json", encoding="utf-8") as fh:
            cfg = Config.from_dict(json.load(fh))
        self.assertEqual(cfg.pending_record_limit, 2)
        self.assertEqual(cfg.max_record_bytes, 65536)
        self.assertEqual(set(cfg.identity_schemes), {"suffix", "casava"})

    def test_utf8_byte_order_sorting(self):
        # UTF-8 字节序：多字节字符的高位字节决定顺序；混入 ASCII 名称。
        names = ["zzz", "éclair", "α", "apple", "中文"]
        r1 = [rec(f"@{n}/1", "A", "I") for n in names]
        r2 = [rec(f"@{n}/2", "T", "!") for n in names]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq"), r2)
        result = self.run_pair([self._src("r1.fq", 1), self._src("r2.fq", 2)])
        expected = sorted((n.encode() for n in names), key=lambda b: b)
        self.assertEqual(result.paired_ids, expected)
        IndependentVerifier(self.out).verify_run()


class TestOrphanVsMateRejected(PairingTestBase):
    def test_single_end_missing_vs_rejected_opposite(self):
        r1 = [
            rec("@orph/1", "A", "I"),                       # 对端根本无记录 -> 孤儿
            rec("@rej/1", "A", "I"),                        # 对端有但被隔离
        ]
        r2 = [
            rec("@rej/2", "AX", "II"),                       # 非法碱基 -> 隔离
            rec("@only2/2", "C", "I"),
        ]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq"), r2)
        result = self.run_pair([self._src("r1.fq", 1), self._src("r2.fq", 2)])
        self.assertEqual({g.read_id for g in result.orphans}, {b"orph", b"only2"})
        self.assertEqual([g.read_id for g in result.mate_rejected], [b"rej"])
        self.assertTrue(any("illegal_nucleotide" in q.reason for q in result.quarantine))
        with open(os.path.join(self.out, ORPHAN_NAME), encoding="utf-8") as fh:
            orph = json.load(fh)
        self.assertEqual({o["read_id_utf8"] for o in orph}, {"orph", "only2"})
        IndependentVerifier(self.out).verify_run()

    def test_illegal_quality_char_isolated_but_parsing_continues(self):
        r1 = [
            rec("@badq/1", "AC", "I\x1f"),                  # 质量含控制字符
            rec("@good/1", "AC", "II"),
        ]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        result = self.run_pair([self._src("r1.fq", 1)])
        self.assertEqual(result.quarantine[0].reason.startswith("illegal_quality_char"), True)
        self.assertEqual([g.read_id for g in result.orphans], [b"good"])
        # 隔离记录仍保留来源位置。
        with open(os.path.join(self.out, QUARANTINE_NAME), encoding="utf-8") as fh:
            q = json.load(fh)
        self.assertIsNotNone(q[0]["byte_range"])


class TestFatalTails(PairingTestBase):
    def _run_incomplete(self, chunks: list[bytes], gz: bool = False):
        name = "tail.fq" + (".gz" if gz else "")
        write_file(os.path.join(self.tmp, name), chunks, gz=gz)
        result = self.run_pair([self._src(name, 1)])
        self.assertFalse(result.complete)
        IndependentVerifier(self.out).verify_run()  # 不完整运行的校验分支
        return result

    def test_quality_truncation_reports_unread_tail(self):
        chunks = [rec("@good/1", "AC", "II"), b"@bad/1\nACGT\n+\nII"]  # 无行尾 EOF
        result = self._run_incomplete(chunks)
        st = result.sources[0]
        self.assertIn("截断", st.fatal.reason)
        self.assertEqual(st.logical_records, 1)  # 已知前缀保留
        # EOF 截断：部分行均已被消费，流位置之后无剩余字节；
        # 但 tail_offset 锚定出错记录起点，诊断仍可定位。
        self.assertEqual(st.fatal.tail_length, 0)
        self.assertEqual(st.fatal.tail_offset, len(rec("@good/1", "AC", "II")))
        self.assertEqual(st.fatal.record_no, 2)

    def test_quality_overflow_stops_file_without_at_resync(self):
        # 质量 5 字符 > 序列 4；即使后面出现一个像样的 @ 头也绝不搜索恢复。
        chunks = [
            rec("@good/1", "AC", "II"),
            b"@ovf/1\nACGT\n+\nIIIII\n@should_not_be_recovered/1\nA\n+\nI\n",
        ]
        result = self._run_incomplete(chunks)
        self.assertIn("超长质量", result.sources[0].fatal.reason)
        # 尾部之后的记录不得进入任何产物。
        all_ids = {g.read_id for g in result.groups}
        self.assertNotIn(b"should_not_be_recovered", all_ids)
        self.assertNotIn(b"ovf", all_ids)

    def test_header_phase_truncation(self):
        chunks = [rec("@good/1", "AC", "II"), b"@no"]
        result = self._run_incomplete(chunks)
        self.assertFalse(result.sources[0].complete)
        self.assertEqual(result.sources[0].logical_records, 1)

    def test_blank_line_is_fatal_not_guesswork(self):
        chunks = [rec("@good/1", "AC", "II"), b"\n@x/1\nA\n+\nI\n"]
        result = self._run_incomplete(chunks)
        self.assertIn("记录头", result.sources[0].fatal.reason)
        # 空行已消费，其后的字节作为未读尾部原样保留（不做 @ 重同步）。
        self.assertEqual(
            base64.b64decode(result.sources[0].fatal_preview_b64),
            b"@x/1\nA\n+\nI\n",
        )

    def test_fatal_tail_inside_gzip_uses_stream_offsets(self):
        chunks = [rec("@good/1", "AC", "II"), b"@bad/1\nAC\n+\nI"]  # 质量缺 1
        result = self._run_incomplete(chunks, gz=True)
        st = result.sources[0]
        self.assertEqual(st.compression, "gzip")
        # 偏移按解压流：good 记录恰好占满前缀长度。
        self.assertEqual(st.fatal.tail_offset, len(rec("@good/1", "AC", "II")))
        with gzip.open(os.path.join(self.tmp, "tail.fq.gz"), "rb") as fh:
            stream = fh.read()
        # 未读尾部起点（当前流位置）+ 剩余长度不超过解压流总长；
        # tail_offset 是出错记录起点，必然不晚于当前流位置。
        self.assertLessEqual(st.fatal.tail_offset + st.fatal.tail_length, len(stream))
        # EOF 截断时剩余未读字节为 0，诊断仍保留记录号与起点偏移。
        self.assertEqual(st.fatal.tail_length, 0)
        self.assertEqual(st.fatal.record_no, 2)


class TestGzipAndOffsets(PairingTestBase):
    def test_gzip_and_plain_mix_window_one(self):
        r1 = [rec(f"@m{i}/1", "AC", "II") for i in range(4)]
        r2 = [rec(f"@m{i}/2", "GT", "JJ") for i in range(4)]
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq.gz"), r2, gz=True)
        result = self.run_pair(
            [self._src("r1.fq", 1), self._src("r2.fq.gz", 2)], window=1
        )
        self.assertTrue(result.complete)
        self.assertEqual(result.paired_count, 4)
        self.assertGreater(result.spill_count, 0)
        IndependentVerifier(self.out).verify_run()

    def test_crlf_line_endings_supported(self):
        data = rec("@c/1", "AC", "II").replace(b"\n", b"\r\n")
        with open(os.path.join(self.tmp, "c.fq"), "wb") as fh:
            fh.write(data)
        result = self.run_pair([self._src("c.fq", 1)])
        self.assertEqual(result.orphans[0].mates[1][0].seq, b"AC")

    def test_gzip_declaration_mismatch_raises(self):
        write_file(os.path.join(self.tmp, "plain.fq"), [rec("@p/1", "A", "I")])
        with self.assertRaises(ValueError):
            self.run_pair([SourceSpec(os.path.join(self.tmp, "plain.fq"), 1, "gzip")])


class TestWindowBound(PairingTestBase):
    def test_memory_never_exceeds_window_under_duplicates(self):
        r1 = []
        r2 = []
        for i in range(12):
            r1.append(rec(f"@u{i}/1", "A", "I"))
            r2.append(rec(f"@u{i}/2", "T", "!"))
        r1.append(rec("@u5/1", "C", "J"))  # 重复身份
        write_file(os.path.join(self.tmp, "r1.fq"), r1)
        write_file(os.path.join(self.tmp, "r2.fq"), r2)
        result = self.run_pair([self._src("r1.fq", 1), self._src("r2.fq", 2)], window=2)
        self.assertLessEqual(result.max_memory_records, 2)
        self.assertEqual(
            next(g for g in result.groups if g.read_id == b"u5").status,
            "ambiguous",
        )
        IndependentVerifier(self.out).verify_run()


if __name__ == "__main__":
    unittest.main(verbosity=2)
