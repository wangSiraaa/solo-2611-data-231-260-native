"""checkpoint 的保存、加载与绑定校验。

checkpoint 绑定以下身份信息，任何一项变化都拒绝沿用旧 checkpoint：

* manifest_id（规范化 manifest 内容哈希）与 manifest 路径；
* config 绑定子集摘要（seed/epoch/worker_count/B/parts/上限/派生与洗牌语义）；
* 源摘要 source_digest（manifest_id + 每个 shard 的实际 sha256 链式哈希）；
* worker_id / worker_count / epoch；
* checkpoint 自身的防篡改哈希（对除 self_sha256 外的规范内容计算）。

保存的运行态足以恢复：下一源位置、尚未输出的洗牌缓冲（成员偏移+摘要）、
RNG 内部状态、已消费数量。恢复时不从头重放。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from .errors import CheckpointError
from .manifest import Config, Manifest, source_digest
from .shuffle import WorkerRunner

CHECKPOINT_VERSION = 1


def build_checkpoint(
    runner: WorkerRunner,
    manifest: Manifest,
    config: Config,
    worker_id: int,
) -> dict[str, object]:
    """根据当前 runner 状态构造 checkpoint 字典（含自校验哈希）。"""
    body: dict[str, object] = {
        "version": CHECKPOINT_VERSION,
        "binding": {
            "manifest_id": manifest.manifest_id,
            "manifest_path": str(manifest.manifest_path),
            "config_digest": config.bound_digest,
            "source_digest": source_digest(manifest),
            "worker_id": worker_id,
            "worker_count": config.worker_count,
            "epoch": config.epoch,
            "seed": config.seed,
        },
        "worker_state": runner.snapshot(),
    }
    body["self_sha256"] = _canonical_hash(body)
    return body


def save_checkpoint(path: str | Path, checkpoint: dict[str, object]) -> None:
    """原子写入 checkpoint，避免半截文件被当作有效续跑点。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=out.name + ".", dir=out.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(checkpoint, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_checkpoint(path: str | Path) -> dict[str, object]:
    p = Path(path)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CheckpointError(f"无法读取 checkpoint {p}: {exc}") from exc
    if not isinstance(raw, dict):
        raise CheckpointError("checkpoint 顶层必须是 JSON 对象")
    stored = raw.get("self_sha256")
    if not isinstance(stored, str):
        raise CheckpointError("checkpoint 缺少 self_sha256")
    actual = _canonical_hash(raw)
    if actual != stored:
        raise CheckpointError("checkpoint 自校验哈希不匹配：文件已被修改或损坏")
    if raw.get("version") != CHECKPOINT_VERSION:
        raise CheckpointError(
            f"checkpoint 版本 {raw.get('version')} 不受支持（当前 {CHECKPOINT_VERSION}）"
        )
    return raw


def verify_binding(
    checkpoint: dict[str, object],
    manifest: Manifest,
    config: Config,
    worker_id: int,
) -> None:
    """逐项核验绑定；任一不匹配都给出明确原因并拒绝恢复。"""
    binding = checkpoint.get("binding")
    if not isinstance(binding, dict):
        raise CheckpointError("checkpoint 缺少 binding 段")
    expected = {
        "manifest_id": manifest.manifest_id,
        "manifest_path": str(manifest.manifest_path),
        "config_digest": config.bound_digest,
        "source_digest": source_digest(manifest),
        "worker_id": worker_id,
        "worker_count": config.worker_count,
        "epoch": config.epoch,
        "seed": config.seed,
    }
    mismatches = [
        key for key, value in expected.items()
        if binding.get(key) != value
    ]
    if mismatches:
        details = "; ".join(
            f"{key}: checkpoint={binding.get(key)!r} current={expected[key]!r}"
            for key in mismatches
        )
        raise CheckpointError(f"checkpoint 绑定失效，拒绝续跑 -> {details}")
    state = checkpoint.get("worker_state")
    if not isinstance(state, dict):
        raise CheckpointError("checkpoint 缺少 worker_state 段")
    for key in ("consumed", "rng_state", "position", "buffer"):
        if key not in state:
            raise CheckpointError(f"worker_state 缺少 {key}")


def resume_runner(
    checkpoint: dict[str, object],
    manifest: Manifest,
    config: Config,
    worker_id: int,
) -> WorkerRunner:
    """先核验绑定再构造 runner；缓冲样本按保存偏移重读原件并核验。"""
    verify_binding(checkpoint, manifest, config, worker_id)
    return WorkerRunner.from_snapshot(
        manifest, config, worker_id, checkpoint["worker_state"]  # type: ignore[arg-type]
    )


def _canonical_hash(checkpoint: dict[str, object]) -> str:
    body = {k: v for k, v in checkpoint.items() if k != "self_sha256"}
    payload = json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
