"""配对引擎：校验、有界暂存、全量检查后裁定并写出规范产物。

关键时序保证：在所有来源文件读取与校验结束之前不发布任何配对结论；
重复身份只有在看到全部输入后才能最终认定为 ambiguous，避免过早输出
随后被推翻的样本。
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass, field

from .identity import IdentityError, resolve_identity
from .model import (
    Config,
    FatalTail,
    QuarantineEntry,
    RawRecord,
    SourceSpec,
    SourceStatus,
    VALID_BASES,
    ValidRecord,
)
from .parser import open_stream, parse_fastq
from .store import BoundedStore
PAIRED_R1_NAME = "paired_R1.fastq"
PAIRED_R2_NAME = "paired_R2.fastq"
MANIFEST_NAME = "manifest.json"
REPORT_NAME = "run_report.json"
ORPHAN_NAME = "orphans.json"
AMBIGUOUS_NAME = "ambiguous.json"
MATE_REJECTED_NAME = "mate_rejected.json"
QUARANTINE_NAME = "quarantine.json"


def _b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _validate_fields(raw: RawRecord) -> str | None:
    """边界已确定的记录做字段合法性检查，返回 None 或拒绝原因。"""
    if len(raw.seq) == 0:
        return "empty_sequence"
    bad = next((ch for ch in raw.seq if ch not in VALID_BASES), None)
    if bad is not None:
        return f"illegal_nucleotide:0x{bad:02x}"
    if len(raw.qual) != len(raw.seq):
        # 解析器保证不会出现超长；此处兜底覆盖零长度等情形。
        return f"quality_length_mismatch:seq={len(raw.seq)},qual={len(raw.qual)}"
    bad_q = next(
        (ch for ch in raw.qual if ch < 33 or ch > 126), None
    )
    if bad_q is not None:
        return f"illegal_quality_char:0x{bad_q:02x}"
    return None


@dataclass
class GroupResult:
    read_id: bytes
    status: str                       # paired / ambiguous / orphan / mate_rejected
    mates: dict[int, list[ValidRecord]] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    complete: bool
    output_dir: str
    sources: list[SourceStatus]
    groups: list[GroupResult]
    quarantine: list[QuarantineEntry]
    spill_count: int
    max_memory_records: int

    @property
    def paired_ids(self) -> list[bytes]:
        return [g.read_id for g in self.groups if g.status == "paired"]

    @property
    def paired_count(self) -> int:
        return len(self.paired_ids)

    @property
    def orphans(self) -> list[GroupResult]:
        return [g for g in self.groups if g.status == "orphan"]

    @property
    def ambiguous(self) -> list[GroupResult]:
        return [g for g in self.groups if g.status == "ambiguous"]

    @property
    def mate_rejected(self) -> list[GroupResult]:
        return [g for g in self.groups if g.status == "mate_rejected"]

    def manifest_entry(self, rec: ValidRecord, status: str, reason: str | None) -> dict:
        src = self.sources[rec.source_index]
        return {
            "source_path": src.path,
            "compression": src.compression,
            "file_sha256": src.file_sha256,
            "logical_record_no": rec.record_no,
            "raw_header_b64": _b64(rec.header),
            "read_id_b64": _b64(rec.read_id),
            "read_id_utf8": rec.read_id.decode("utf-8", errors="replace"),
            "mate": rec.mate,
            "status": status,
            "reason": reason,
            "byte_range": {
                "space": "decompressed_fastq_stream"
                if src.compression == "gzip"
                else "raw_file_bytes",
                "start": rec.start_offset,
                "end": rec.end_offset,
            },
        }


def run_pairing(
    sources: list[SourceSpec],
    config: Config,
    output_dir: str,
    sqlite_path: str | None = None,
) -> RunResult:
    os.makedirs(output_dir, exist_ok=True)
    db_path = sqlite_path or os.path.join(output_dir, ".staging.sqlite")
    store = BoundedStore(config.pending_record_limit, db_path)
    statuses: list[SourceStatus] = []
    quarantine: list[QuarantineEntry] = []
    max_memory = 0

    # ---- 阶段 A：逐个来源完整读取与校验（此阶段不发布任何配对） ----
    for idx, spec in enumerate(sources):
        stream = open_stream(spec.path, spec.compression)
        st = SourceStatus(
            source_index=idx,
            path=spec.path,
            compression=stream.compression,
            file_sha256=stream.file_sha256,
            default_mate=spec.default_mate,
        )
        try:
            for raw in parse_fastq(stream, config.max_record_bytes):
                st.logical_records += 1
                field_reject = _validate_fields(raw)

                # 身份解释独立于字段校验：即使序列/质量非法，只要头部能解释出
                # 身份，隔离记录就携带 read_id，供对端 mate_rejected 判定使用。
                read_id: bytes | None = None
                declared_mate: int | None = None
                identity_reject: str | None = None
                try:
                    read_id, declared_mate = resolve_identity(
                        raw.header, spec.default_mate, config
                    )
                except IdentityError as exc:
                    identity_reject = f"identity:{exc}"
                reject = field_reject or identity_reject

                if reject is not None:
                    quarantine.append(
                        QuarantineEntry(
                            record_no=raw.record_no,
                            source_index=idx,
                            header=raw.header,
                            reason=reject,
                            read_id=read_id,
                            declared_mate=declared_mate,
                            seq=raw.seq,
                            qual=raw.qual,
                            start_offset=raw.start_offset,
                            end_offset=raw.end_offset,
                        )
                    )
                    st.quarantined_count += 1
                    continue

                rec = ValidRecord(
                    record_no=raw.record_no,
                    read_id=read_id,           # type: ignore[arg-type]
                    mate=declared_mate,        # type: ignore[arg-type]
                    header=raw.header,
                    seq=raw.seq,
                    qual=raw.qual,
                    start_offset=raw.start_offset,
                    end_offset=raw.end_offset,
                    source_index=idx,
                )
                store.add(rec)
                st.valid_count += 1
                max_memory = max(max_memory, store.memory_records)
        except FatalTail as fatal:
            # 文件级致命错误：停止该文件，保留未读尾部诊断；其余来源继续。
            st.fatal = fatal
            st.fatal_tail_offset = fatal.tail_offset
            st.fatal_tail_length = fatal.tail_length
            st.fatal_preview_b64 = _b64(fatal.preview)
            st.complete = False
        finally:
            stream.binary.close()
        statuses.append(st)

    complete = all(st.complete for st in statuses)

    # ---- 阶段 B：全部输入检查结束，落盘完整候选集后统一裁定 ----
    store.flush()
    max_memory = max(max_memory, store.memory_records)

    # 隔离记录按可解释身份建索引，用于“另一端有拒绝记录”的区分。
    quarantined_ids: set[bytes] = {
        q.read_id for q in quarantine if q.read_id is not None
    }

    groups: list[GroupResult] = []
    for read_id in store.iter_all_ids():
        mates: dict[int, list[ValidRecord]] = {1: [], 2: []}
        for rec in store.iter_group(read_id):
            mates.setdefault(rec.mate, []).append(rec)
        n1, n2 = len(mates.get(1, [])), len(mates.get(2, []))

        if n1 > 1 or n2 > 1:
            # 任一 mate 出现多个同身份记录：整组 ambiguous，绝不做多对多。
            grp = GroupResult(
                read_id,
                "ambiguous",
                mates,
                [f"duplicate_mate_records:mate1={n1},mate2={n2}"],
            )
        elif n1 == 1 and n2 == 1:
            grp = GroupResult(read_id, "paired", mates)
        elif (n1 == 1 and n2 == 0) or (n1 == 0 and n2 == 1):
            if read_id in quarantined_ids:
                grp = GroupResult(
                    read_id,
                    "mate_rejected",
                    mates,
                    ["opposite_mate_record_quarantined"],
                )
            else:
                grp = GroupResult(
                    read_id,
                    "orphan",
                    mates,
                    ["single_end_missing"],
                )
        else:  # 理论上不会发生：组内至少有一条合法记录
            grp = GroupResult(read_id, "ambiguous", mates, ["unexpected_empty_group"])
        groups.append(grp)

    # UTF-8 字节序：直接按 read_id 字节排序（合法 UTF-8 与码点序一致）。
    groups.sort(key=lambda g: g.read_id)

    result = RunResult(
        complete=complete,
        output_dir=output_dir,
        sources=statuses,
        groups=groups,
        quarantine=quarantine,
        spill_count=store.spill_count,
        max_memory_records=max_memory,
    )

    _write_outputs(result, config)
    store.close()
    return result


def _write_fastq(path: str, records: list[ValidRecord]) -> None:
    """写规范四行 FASTQ：原头保留，多行 seq/qual 合成单行，值不改动。"""
    with open(path, "wb") as fh:
        for rec in records:
            fh.write(rec.header + b"\n")
            fh.write(rec.seq + b"\n")
            fh.write(b"+\n")
            fh.write(rec.qual + b"\n")


def _group_payload(grp: GroupResult, result: RunResult) -> dict:
    return {
        "read_id_b64": _b64(grp.read_id),
        "read_id_utf8": grp.read_id.decode("utf-8", errors="replace"),
        "status": grp.status,
        "reasons": grp.reasons,
        "records": [
            result.manifest_entry(rec, grp.status, grp.reasons[0] if grp.reasons else None)
            for mate in sorted(grp.mates)
            for rec in grp.mates[mate]
        ],
    }


def _write_outputs(result: RunResult, config: Config) -> None:
    out = result.output_dir

    # ---- 配对 FASTQ 仅在输入完整（complete=true）时发布 ----
    if result.complete:
        r1_records: list[ValidRecord] = []
        r2_records: list[ValidRecord] = []
        for grp in result.groups:
            if grp.status != "paired":
                continue
            r1_records.append(grp.mates[1][0])
            r2_records.append(grp.mates[2][0])
        _write_fastq(os.path.join(out, PAIRED_R1_NAME), r1_records)
        _write_fastq(os.path.join(out, PAIRED_R2_NAME), r2_records)

    # ---- 孤儿、ambiguous、mate 拒绝清单 ----
    with open(os.path.join(out, ORPHAN_NAME), "w", encoding="utf-8") as fh:
        json.dump([_group_payload(g, result) for g in result.orphans],
                  fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out, AMBIGUOUS_NAME), "w", encoding="utf-8") as fh:
        json.dump([_group_payload(g, result) for g in result.ambiguous],
                  fh, ensure_ascii=False, indent=2)
    with open(os.path.join(out, MATE_REJECTED_NAME), "w", encoding="utf-8") as fh:
        json.dump([_group_payload(g, result) for g in result.mate_rejected],
                  fh, ensure_ascii=False, indent=2)

    # ---- 隔离清单：保留来源位置与原始头 ----
    q_payload = []
    for q in result.quarantine:
        src = result.sources[q.source_index]
        q_payload.append({
            "source_path": src.path,
            "compression": src.compression,
            "file_sha256": src.file_sha256,
            "logical_record_no": q.record_no,
            "raw_header_b64": _b64(q.header),
            "read_id_b64": _b64(q.read_id),
            "declared_mate": q.declared_mate,
            "reason": q.reason,
            "seq_b64": _b64(q.seq),
            "qual_b64": _b64(q.qual),
            "byte_range": None
            if q.start_offset is None
            else {
                "space": "decompressed_fastq_stream"
                if src.compression == "gzip"
                else "raw_file_bytes",
                "start": q.start_offset,
                "end": q.end_offset,
            },
        })
    with open(os.path.join(out, QUARANTINE_NAME), "w", encoding="utf-8") as fh:
        json.dump(q_payload, fh, ensure_ascii=False, indent=2)

    # ---- 总清单：每个来源文件、逻辑记录号、原头、规范身份、最终状态 ----
    manifest = {
        "complete": result.complete,
        "config": {
            "pending_record_limit": config.pending_record_limit,
            "max_record_bytes": config.max_record_bytes,
            "identity_schemes": list(config.identity_schemes),
        },
        "sources": [_source_summary(st) for st in result.sources],
        "records": _all_records_manifest(result),
    }
    with open(os.path.join(out, MANIFEST_NAME), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    # ---- 运行报告 ----
    report = {
        "complete": result.complete,
        "counts": {
            "paired_pairs": result.paired_count,
            "orphan_groups": len(result.orphans),
            "ambiguous_groups": len(result.ambiguous),
            "mate_rejected_groups": len(result.mate_rejected),
            "quarantined_records": len(result.quarantine),
            "spilled_records": result.spill_count,
            "max_memory_pending_records": result.max_memory_records,
        },
        "sources": [_source_summary(st) for st in result.sources],
        "paired_read_ids_b64": [_b64(rid) for rid in result.paired_ids],
    }
    with open(os.path.join(out, REPORT_NAME), "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2)


def _all_records_manifest(result: RunResult) -> list[dict]:
    """总清单：合法记录按 read_id 字节序、再按 mate 排列，附最终状态。"""
    entries: list[dict] = []
    for grp in result.groups:
        reason = grp.reasons[0] if grp.reasons else None
        for mate in sorted(grp.mates):
            for rec in grp.mates[mate]:
                entries.append(result.manifest_entry(rec, grp.status, reason))
    return entries


def _source_summary(st: SourceStatus) -> dict:
    return {
        "source_index": st.source_index,
        "path": st.path,
        "compression": st.compression,
        "file_sha256": st.file_sha256,
        "default_mate": st.default_mate,
        "logical_records": st.logical_records,
        "valid_records": st.valid_count,
        "quarantined_records": st.quarantined_count,
        "complete": st.complete,
        "fatal_tail": None
        if st.fatal is None
        else {
            "reason": st.fatal.reason,
            "record_no": st.fatal.record_no,
            "tail_offset": st.fatal_tail_offset,
            "tail_length": st.fatal_tail_length,
            "preview_b64": st.fatal_preview_b64,
        },
    }
