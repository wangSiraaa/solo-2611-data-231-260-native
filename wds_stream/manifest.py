"""manifest / config 加载、源摘要与确定性 seed 派生。

manifest 为 JSON 列表，每项 ``{"shard_id", "path", "sha256"}``，顺序即 shard 序号。
config 的字段与 fixtures/config.json 对齐；seed 派生公式按 config 中写明的规则实现：

    payload = json.dumps([seed, epoch, worker_id], separators=(",", ":")) 的 UTF-8 字节
    digest  = sha256(payload)
    worker_seed = int.from_bytes(digest[:8], "big", signed=False)
    rng = random.Random(worker_seed)
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

# 进入 checkpoint/config 摘要的配置键；任何一个变化都会使旧 checkpoint 失效。
BOUND_CONFIG_KEYS = (
    "seed",
    "epoch",
    "worker_count",
    "buffer_samples",
    "parts",
    "max_member_bytes",
    "seed_derivation",
    "shuffle",
)


@dataclass(frozen=True)
class ShardEntry:
    """manifest 中的一个 shard。"""

    index: int
    shard_id: str
    path: Path
    sha256: str


@dataclass(frozen=True)
class Manifest:
    """已解析的 manifest，路径全部相对 base_dir 解析为绝对路径。"""

    base_dir: Path
    manifest_path: Path
    manifest_id: str  # 规范化 manifest 内容的 sha256，checkpoint 绑定用
    shards: tuple[ShardEntry, ...]

    def shards_for_worker(self, worker_id: int, worker_count: int) -> tuple[ShardEntry, ...]:
        """按 manifest 顺序把 shard 序号模 worker_count 分给 worker_id。

        各 worker 只拿到自己的 shard，物理上不会打开其他 worker 的 tar。
        """
        if not 0 <= worker_id < worker_count:
            raise ConfigError(f"worker_id={worker_id} 超出 [0,{worker_count})")
        return tuple(s for i, s in enumerate(self.shards) if i % worker_count == worker_id)


@dataclass(frozen=True)
class Config:
    """洗牌运行配置（fixtures/config.json 的内存表示）。"""

    buffer_samples: int
    worker_count: int
    seed: int
    epoch: int
    parts: tuple[str, ...]
    max_member_bytes: int
    seed_derivation: str
    shuffle: str
    raw: dict[str, Any]

    @property
    def bound_digest(self) -> str:
        """参与 checkpoint 绑定的配置子集摘要（只含 BOUND_CONFIG_KEYS）。"""
        payload = json.dumps(
            {k: self.raw[k] for k in BOUND_CONFIG_KEYS},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


def _canonical_manifest(raw: Any) -> bytes:
    """规范化 manifest：保留绑定所需的三列并按列表顺序序列化，忽略排版差异。"""
    if not isinstance(raw, list) or not raw:
        raise ConfigError("manifest 必须是非空 JSON 列表")
    rows = []
    for item in raw:
        if not isinstance(item, dict):
            raise ConfigError("manifest 每项必须是对象")
        try:
            rows.append(
                {
                    "shard_id": str(item["shard_id"]),
                    "path": str(item["path"]),
                    "sha256": str(item["sha256"]),
                }
            )
        except KeyError as exc:
            raise ConfigError(f"manifest 项缺少字段 {exc}") from exc
    return json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def load_manifest(manifest_path: str | Path) -> Manifest:
    path = Path(manifest_path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取 manifest {path}: {exc}") from exc
    manifest_id = hashlib.sha256(_canonical_manifest(raw)).hexdigest()
    base_dir = path.parent
    shards: list[ShardEntry] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        shard_id = str(item["shard_id"])
        if shard_id in seen_ids:
            raise ConfigError(f"manifest 中 shard_id 重复: {shard_id}")
        seen_ids.add(shard_id)
        sha = str(item["sha256"]).lower()
        if len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
            raise ConfigError(f"shard {shard_id} 的 sha256 不是 64 位十六进制")
        shard_path = (base_dir / str(item["path"])).resolve()
        shards.append(ShardEntry(index, shard_id, shard_path, sha))
    return Manifest(base_dir=base_dir, manifest_path=path, manifest_id=manifest_id,
                    shards=tuple(shards))


def load_config(config_path: str | Path) -> Config:
    path = Path(config_path).resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"无法读取 config {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config 必须是 JSON 对象")
    required = set(BOUND_CONFIG_KEYS)
    missing = required - raw.keys()
    if missing:
        raise ConfigError(f"config 缺少字段: {sorted(missing)}")
    parts = raw["parts"]
    if not isinstance(parts, list) or not parts or any(not isinstance(p, str) for p in parts):
        raise ConfigError("config.parts 必须是非空字符串列表")
    if len(set(parts)) != len(parts):
        raise ConfigError("config.parts 存在重复部件名")
    try:
        buffer_samples = int(raw["buffer_samples"])
        worker_count = int(raw["worker_count"])
        seed = int(raw["seed"])
        epoch = int(raw["epoch"])
        max_member_bytes = int(raw["max_member_bytes"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"config 整数字段非法: {exc}") from exc
    if buffer_samples <= 0 or worker_count <= 0 or max_member_bytes <= 0:
        raise ConfigError("buffer_samples / worker_count / max_member_bytes 必须为正数")
    return Config(
        buffer_samples=buffer_samples,
        worker_count=worker_count,
        seed=seed,
        epoch=epoch,
        parts=tuple(parts),
        max_member_bytes=max_member_bytes,
        seed_derivation=str(raw["seed_derivation"]),
        shuffle=str(raw["shuffle"]),
        raw=raw,
    )


def worker_seed(seed: int, epoch: int, worker_id: int) -> int:
    """按 config 写明的 SHA256 派生规则生成 worker 确定 seed（见模块 docstring）。"""
    payload = json.dumps([seed, epoch, worker_id], separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def make_rng(seed: int, epoch: int, worker_id: int) -> random.Random:
    """每个 worker 独立的 random.Random，seed 派生方式由 config 固定。"""
    return random.Random(worker_seed(seed, epoch, worker_id))


def source_digest(manifest: Manifest) -> str:
    """源摘要：manifest_id 与每个 shard 实际 sha256（按 manifest 顺序）的链式哈希。

    任一 shard 内容被替换都会改变该摘要；checkpoint 用它拒绝源变动。
    """
    h = hashlib.sha256()
    h.update(b"manifest:")
    h.update(manifest.manifest_id.encode("ascii"))
    for shard in manifest.shards:
        h.update(b"|shard:")
        h.update(shard.shard_id.encode("utf-8"))
        h.update(b":")
        h.update(_sha256_file(shard.path).encode("ascii"))
    return h.hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_shard_hash(shard: ShardEntry) -> None:
    """打开 shard 前核对 manifest 记录的 sha256，不匹配即显式失败。"""
    actual = _sha256_file(shard.path)
    if actual != shard.sha256:
        raise ConfigError(
            f"shard {shard.shard_id} 哈希不匹配: manifest={shard.sha256} actual={actual}"
        )
