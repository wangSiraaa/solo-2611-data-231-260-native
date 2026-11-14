"""只读审计：合并覆盖、序列逐项一致性、checkpoint 绑定。

审计不依赖运行器的输出自报，而是直接重新流式枚举 manifest 中的全部合法样本，
得到期望身份集合，再与 worker 输出独立比对。
"""

from __future__ import annotations

import json
from pathlib import Path

from .checkpoint import load_checkpoint, verify_binding
from .errors import CheckpointError, WDSStreamError
from .manifest import Config, Manifest
from .tarstream import SampleStream


def expected_sample_ids(manifest: Manifest, config: Config) -> list[str]:
    """独立枚举全部合法样本身份（单 worker 视角读全部 shard，仅用于审计期望集）。"""
    with SampleStream(manifest.shards, config.parts, config.max_member_bytes) as stream:
        return [sample.sample_id for sample in stream]


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise WDSStreamError(f"{path}:{lineno} 不是合法 JSON: {exc}") from exc
        if not isinstance(record, dict) or "sample_id" not in record:
            raise WDSStreamError(f"{path}:{lineno} 缺少 sample_id 字段")
        records.append(record)
    return records


def audit_merge(
    manifest: Manifest,
    config: Config,
    output_paths: list[str | Path],
) -> dict[str, object]:
    """独立合并所有 worker 输出：恰好包含全部合法样本，每个一次。"""
    expected = expected_sample_ids(manifest, config)
    seen: list[str] = []
    per_file: dict[str, list[str]] = {}
    for path in output_paths:
        p = Path(path)
        records = _read_jsonl(p)
        ids = [str(r["sample_id"]) for r in records]
        per_file[str(p)] = ids
        seen.extend(ids)

    expected_set = set(expected)
    actual_set = set(seen)
    missing = sorted(expected_set - actual_set)
    unexpected = sorted(actual_set - expected_set)
    duplicates = sorted(sid for sid in actual_set if seen.count(sid) > 1)
    ok = not missing and not unexpected and not duplicates and len(seen) == len(expected)

    # 每条输出记录的部件摘要必须与规定部件集合一致。
    bad_parts: list[str] = []
    for path in output_paths:
        for record in _read_jsonl(Path(path)):
            part_names = [str(item["part"]) for item in record["parts"]]  # type: ignore[index]
            if sorted(part_names) != sorted(config.parts):
                bad_parts.append(str(record["sample_id"]))
    if bad_parts:
        ok = False
    return {
        "ok": ok,
        "expected_count": len(expected),
        "actual_count": len(seen),
        "missing": missing,
        "unexpected": unexpected,
        "duplicates": duplicates,
        "bad_part_sets": sorted(set(bad_parts)),
        "per_file_counts": {k: len(v) for k, v in per_file.items()},
    }


def audit_sequence(
    full_path: str | Path,
    resumed_path: str | Path,
) -> dict[str, object]:
    """同 seed/epoch/config 下，不中断序列与“保存后恢复”序列必须逐项完全相同。

    比较 sample_id 与部件摘要（含 sha256/偏移），消费次序也必须一致。
    """
    full_records = _read_jsonl(Path(full_path))
    resumed_records = _read_jsonl(Path(resumed_path))
    first_diff: dict[str, object] | None = None
    for index, (a, b) in enumerate(zip(full_records, resumed_records)):
        if a != b:
            first_diff = {
                "position": index + 1,
                "uninterrupted": {"sample_id": a.get("sample_id"), "parts": a.get("parts")},
                "resumed": {"sample_id": b.get("sample_id"), "parts": b.get("parts")},
            }
            break
    ok = first_diff is None and len(full_records) == len(resumed_records)
    return {
        "ok": ok,
        "full_count": len(full_records),
        "resumed_count": len(resumed_records),
        "first_diff": first_diff,
    }


def audit_checkpoint(
    checkpoint_path: str | Path,
    manifest: Manifest,
    config: Config,
    worker_id: int,
) -> dict[str, object]:
    """核验 checkpoint 自哈希与全部绑定项；绑定失效时返回明确原因而非通过。"""
    try:
        checkpoint = load_checkpoint(checkpoint_path)
        verify_binding(checkpoint, manifest, config, worker_id)
    except CheckpointError as exc:
        return {"ok": False, "reason": str(exc)}
    state = checkpoint["worker_state"]
    return {
        "ok": True,
        "consumed": state["consumed"],
        "buffered": len(state["buffer"]),
        "position": state["position"],
        "binding": checkpoint["binding"],
    }
