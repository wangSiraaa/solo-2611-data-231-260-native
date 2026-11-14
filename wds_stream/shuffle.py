"""B 个样本上限的流式有界洗牌与可恢复运行器。

洗牌语义严格按 config 文字实现：

1. 先从源顺序取完整样本，装满最多 B 个缓冲（填充期不输出、不调用 RNG）；
2. 之后每得到一个新样本，``randrange(len(buffer))`` 选一个位置，
   先输出该位置样本，再用新样本替换它；
3. 源结束后，反复 ``randrange`` 选位置并 ``pop``，直到缓冲清空。

内存中持有的完整样本数除当前正在 tar 流里组装的那一个外，始终不超过 B。
绝不把全部样本先装入内存再 shuffle，也不做固定排序伪装随机。
"""

from __future__ import annotations

import hashlib
import tarfile
from dataclasses import dataclass
from typing import Iterator

from .errors import CheckpointError
from .manifest import Config, Manifest, ShardEntry, make_rng
from .tarstream import Part, Sample, SampleStream, StreamPosition


@dataclass
class BufferEntry:
    """洗牌缓冲中一个样本的可序列化形态（不含原始字节，只留原件定位与摘要）。"""

    shard_index: int
    shard_id: str
    key: str
    # part -> {size, sha256, header_offset, data_offset}
    parts: dict[str, dict[str, int | str]]

    @property
    def sample_id(self) -> str:
        return f"{self.shard_id}/{self.key}"

    def to_dict(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "shard_index": self.shard_index,
            "shard_id": self.shard_id,
            "key": self.key,
            "parts": [
                {"part": part, **self.parts[part]} for part in sorted(self.parts)
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> "BufferEntry":
        parts = {
            str(item["part"]): {
                "size": int(item["size"]),
                "sha256": str(item["sha256"]),
                "header_offset": int(item["header_offset"]),
                "data_offset": int(item["data_offset"]),
            }
            for item in raw["parts"]  # type: ignore[index]
        }
        return cls(
            shard_index=int(raw["shard_index"]),  # type: ignore[arg-type]
            shard_id=str(raw["shard_id"]),
            key=str(raw["key"]),
            parts=parts,
        )


def _sample_to_entry(sample: Sample) -> BufferEntry:
    return BufferEntry(
        shard_index=sample.shard_index,
        shard_id=sample.shard_id,
        key=sample.key,
        parts={
            name: {
                "size": part.size,
                "sha256": part.sha256,
                "header_offset": part.header_offset,
                "data_offset": part.data_offset,
            }
            for name, part in sample.parts.items()
        },
    )


def _rebuild_sample(
    entry: BufferEntry,
    shards: tuple[ShardEntry, ...],
    expected_parts: tuple[str, ...],
    max_member_bytes: int,
) -> Sample:
    """按 checkpoint 中保存的成员偏移重新打开 tar 读取缓冲样本原件并核验。

    只读这一个样本自己的成员，不重放源、也不经过主流位置；
    成员名、大小、sha256 全部核验，任何篡改都拒绝恢复。
    """
    by_index = {s.index: s for s in shards}
    if entry.shard_index not in by_index:
        raise CheckpointError(
            f"缓冲样本 {entry.sample_id} 所属 shard {entry.shard_index} 不在当前 worker 名下"
        )
    shard = by_index[entry.shard_index]
    from .manifest import verify_shard_hash

    verify_shard_hash(shard)
    if set(entry.parts) != set(expected_parts):
        raise CheckpointError(
            f"缓冲样本 {entry.sample_id} 部件集合 {sorted(entry.parts)} "
            f"与规定部件 {list(expected_parts)} 不一致"
        )
    sample = Sample(shard_index=shard.index, shard_id=shard.shard_id, key=entry.key)
    for part_name in expected_parts:
        info = entry.parts[part_name]
        offset = int(info["header_offset"])
        expected_name = f"{entry.key}.{part_name}"
        try:
            with shard.path.open("rb") as fh:
                fh.seek(offset)
                with tarfile.open(fileobj=fh, mode="r|") as tf:
                    member = next(iter(tf))
                    if member.name != expected_name:
                        raise CheckpointError(
                            f"偏移 {offset} 处成员为 {member.name!r}，"
                            f"与 checkpoint 记录 {expected_name!r} 不符"
                        )
                    raw = tf.extractfile(member).read()
        except (OSError, tarfile.TarError, StopIteration) as exc:
            raise CheckpointError(
                f"无法在 shard {shard.shard_id} 偏移 {offset} 重建 {expected_name}: {exc}"
            ) from exc
        if member.size != int(info["size"]) or len(raw) != member.size:
            raise CheckpointError(f"重建 {expected_name} 时大小核验失败")
        if member.size > max_member_bytes:
            raise CheckpointError(f"重建 {expected_name} 时发现成员超过大小上限")
        digest = hashlib.sha256(raw).hexdigest()
        if digest != str(info["sha256"]):
            raise CheckpointError(
                f"重建 {expected_name} 时 sha256 核验失败：源内容相对 checkpoint 已变化"
            )
        sample.parts[part_name] = Part(
            part=part_name,
            data=raw,
            size=len(raw),
            sha256=digest,
            header_offset=offset,
            data_offset=int(info["data_offset"]),
        )
    return sample


class WorkerRunner:
    """单 worker 的流式洗牌运行器，可在任意输出点快照并从快照恢复。"""

    def __init__(
        self,
        stream: SampleStream,
        config: Config,
        worker_id: int,
        rng,
        buffer: list[Sample] | None = None,
        consumed: int = 0,
    ) -> None:
        self._stream = stream
        self._config = config
        self.worker_id = worker_id
        self._rng = rng
        self.buffer: list[Sample] = list(buffer or [])
        if len(self.buffer) > config.buffer_samples:
            raise ValueError("初始缓冲超过 B")
        self.consumed = consumed

    # ------------------------------------------------------------ 构造入口
    @classmethod
    def fresh(cls, manifest: Manifest, config: Config, worker_id: int) -> "WorkerRunner":
        shards = manifest.shards_for_worker(worker_id, config.worker_count)
        stream = SampleStream(shards, config.parts, config.max_member_bytes)
        rng = make_rng(config.seed, config.epoch, worker_id)
        return cls(stream, config, worker_id, rng)

    @classmethod
    def from_snapshot(
        cls,
        manifest: Manifest,
        config: Config,
        worker_id: int,
        state: dict[str, object],
    ) -> "WorkerRunner":
        """从 checkpoint 的 worker 状态恢复：缓冲按偏移重建，RNG/位置/计数还原。"""
        shards = manifest.shards_for_worker(worker_id, config.worker_count)
        stream = SampleStream(shards, config.parts, config.max_member_bytes)
        pos_raw = state["position"]
        stream.seek(
            StreamPosition(
                shard_index=int(pos_raw["shard_index"]),  # type: ignore[index]
                offset=int(pos_raw["offset"]),  # type: ignore[index]
            )
        )
        rng = make_rng(config.seed, config.epoch, worker_id)
        rng.setstate(_json_to_rng_state(state["rng_state"]))
        entries = [BufferEntry.from_dict(item) for item in state["buffer"]]  # type: ignore[arg-type]
        buffer = [
            _rebuild_sample(entry, shards, config.parts, config.max_member_bytes)
            for entry in entries
        ]
        runner = cls(
            stream,
            config,
            worker_id,
            rng,
            buffer=buffer,
            consumed=int(state["consumed"]),
        )
        return runner

    # ------------------------------------------------------------ 主循环
    def run(self) -> Iterator[dict[str, object]]:
        """生成输出记录（1-based seq 在记录内）。

        生成器在每次 yield 处挂起，此时实例状态完整，可随时 :meth:`snapshot`。
        """
        B = self._config.buffer_samples
        try:
            for sample in self._stream:
                if len(self.buffer) < B:
                    # 填充期：只入缓冲，不输出、不调用 RNG。
                    self.buffer.append(sample)
                    continue
                # 替换期：先随机选位输出，再用新样本替换。
                index = self._rng.randrange(len(self.buffer))
                chosen = self.buffer[index]
                self.buffer[index] = sample
                yield self._emit(chosen)
            # 源结束：随机弹出直到清空。
            while self.buffer:
                index = self._rng.randrange(len(self.buffer))
                chosen = self.buffer.pop(index)
                yield self._emit(chosen)
        finally:
            # 装配失败等异常路径也要释放 tar 文件句柄。
            self._stream.close()

    def _emit(self, sample: Sample) -> dict[str, object]:
        self.consumed += 1
        return {
            "seq": self.consumed,
            "sample_id": sample.sample_id,
            "shard_id": sample.shard_id,
            "shard_index": sample.shard_index,
            "key": sample.key,
            "parts": [
                {
                    "part": name,
                    "size": sample.parts[name].size,
                    "sha256": sample.parts[name].sha256,
                    "header_offset": sample.parts[name].header_offset,
                    "data_offset": sample.parts[name].data_offset,
                }
                for name in self._config.parts
            ],
        }

    # ------------------------------------------------------------ 快照
    def snapshot(self) -> dict[str, object]:
        """保存恢复所需全部运行态：源位置、未输出缓冲、RNG、已消费数量。"""
        position = self._stream.position()
        return {
            "consumed": self.consumed,
            "rng_state": _rng_state_to_json(self._rng.getstate()),
            "position": {"shard_index": position.shard_index, "offset": position.offset},
            "buffer": [_sample_to_entry(s).to_dict() for s in self.buffer],
        }

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "WorkerRunner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


# random.Random 的内部状态是嵌套 tuple，JSON 不支持 tuple，需递归转换。
def _rng_state_to_json(state) -> object:
    if isinstance(state, tuple):
        return [_rng_state_to_json(x) for x in state]
    if isinstance(state, list):
        return [_rng_state_to_json(x) for x in state]
    return state


def _json_to_rng_state(value):
    if isinstance(value, list):
        converted = tuple(_json_to_rng_state(x) for x in value)
        # getstate 顶层为 (version, internalstate_tuple, gauss_cached)
        return converted
    return value
