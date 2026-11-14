"""按 tar 成员流组装 WebDataset 样本。

约束（与任务说明一致）：

* 使用标准库 ``tarfile`` 的 ``r|`` 流式模式，自己不实现 tar 编解码；
* 成员必须连续排列、文件名为 ``key.part``，key 只含字母数字下划线；
* 完整样本必须恰好包含 config 规定的全部部件，缺件 / 重复部件 / 非法路径 /
  超大小都抛出 :class:`DataAssemblyError`，worker 明确失败；
* 不允许把下一个 key 的部件补给上一个未完成样本——key 切换时上一样本若不完整
  立即报错；
* 内存中除“当前正在组装的一个样本”外，不保留其他原始部件；
* 源位置以 ``(shard 索引, tar 头绝对字节偏移)`` 表示，可在该偏移重新开流恢复，
  无需从头重放。
"""

from __future__ import annotations

import hashlib
import re
import tarfile
from dataclasses import dataclass, field
from typing import Iterator

from .errors import DataAssemblyError
from .manifest import ShardEntry, verify_shard_hash

# key 只允许字母数字下划线；part 也取简单 token，禁止任何路径分隔。
_MEMBER_RE = re.compile(r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$")
_BLOCK = 512


class _ShardBoundary(Exception):
    """内部信号：当前 shard 成员流已干净结束，需要推进到下一个 shard。"""


@dataclass(frozen=True)
class Part:
    """单个部件的原件与摘要。"""

    part: str
    data: bytes
    size: int
    sha256: str
    # 该部件 tar 头在所属 shard 中的绝对字节偏移（恢复 / 审计用）。
    header_offset: int
    data_offset: int

    def digest(self) -> dict[str, object]:
        return {
            "part": self.part,
            "size": self.size,
            "sha256": self.sha256,
            "header_offset": self.header_offset,
            "data_offset": self.data_offset,
        }


@dataclass
class Sample:
    """一个完整样本：身份 = shard 身份 + key，不能只用 key。"""

    shard_index: int
    shard_id: str
    key: str
    parts: dict[str, Part] = field(default_factory=dict)

    @property
    def sample_id(self) -> str:
        return f"{self.shard_id}/{self.key}"

    def digest(self) -> dict[str, object]:
        """输出用的部件摘要（按 config 部件顺序，不含原始字节）。"""
        return {
            "sample_id": self.sample_id,
            "shard_id": self.shard_id,
            "shard_index": self.shard_index,
            "key": self.key,
            "parts": [self.parts[name].digest() for name in sorted(self.parts)],
        }


@dataclass(frozen=True)
class StreamPosition:
    """tar 源中的一个可恢复位置：某个 shard 头部的绝对字节偏移。

    ``shard_index`` 是 manifest 级索引（不是 worker 内的相对序号），
    offset 指向“下一个尚未消费的成员头”；shard 尚未开始时 offset 为 0。
    """

    shard_index: int
    offset: int


class SampleStream:
    """对单个 worker 所属 shard 序列做流式样本组装。

    参数
    ----
    shards: 该 worker 按 manifest 顺序拥有的 shard（其他 shard 不会被打开）。
    parts: config 规定的完整部件集合，顺序即校验顺序。
    max_member_bytes: 单个成员原始字节上限，超过即失败。
    """

    def __init__(
        self,
        shards: tuple[ShardEntry, ...],
        parts: tuple[str, ...],
        max_member_bytes: int,
    ) -> None:
        if not shards:
            raise ValueError("SampleStream 至少需要一个 shard")
        self._shards = shards
        self._parts = parts
        self._part_set = set(parts)
        self._max_member_bytes = max_member_bytes
        # 下一个要读取的 shard 在 self._shards 中的位置。
        self._next_shard_pos = 0
        # 当前 shard 内下一个成员头的绝对偏移；shard 未开始时为 0。
        self._next_offset = 0
        self._fh = None
        self._tf: tarfile.TarFile | None = None
        self._members: Iterator[tarfile.TarInfo] | None = None
        # 当前 tar 流相对 shard 起点的基准偏移（seek 续读时非 0）。
        self._stream_base = 0
        # 当前 shard 内下一个成员头的绝对偏移；不能用 fileobj.tell()：
        # r| 模式 tarfile 带内部读缓冲，tell() 会越过下一个头。
        # 精确值 = 流基准 + TarInfo.offset + 头块512 + 数据向上取整到512。
        self._current: Sample | None = None

    # ------------------------------------------------------------------ 位置
    @property
    def exhausted(self) -> bool:
        """所有 shard 均已干净读完。"""
        return self._next_shard_pos >= len(self._shards)

    def position(self) -> StreamPosition:
        """当前可恢复位置（下一个未消费成员头）。"""
        if self.exhausted:
            # 源已耗尽：用最后一个 shard 的文件尾标记完成态。
            last = self._shards[-1]
            return StreamPosition(last.index, last.path.stat().st_size)
        return StreamPosition(self._shards[self._next_shard_pos].index, self._next_offset)

    def seek(self, position: StreamPosition) -> None:
        """绑定到已保存的位置，后续迭代从该成员头开始，不重放此前成员。"""
        for rel, shard in enumerate(self._shards):
            if shard.index == position.shard_index:
                break
        else:
            raise DataAssemblyError(
                f"checkpoint 位置指向 shard {position.shard_index}，"
                f"不属于当前 worker 的 shard 集合"
            )
        self.close()
        self._next_shard_pos = rel
        self._next_offset = int(position.offset)
        self._stream_base = 0
        self._current = None

    def close(self) -> None:
        if self._tf is not None:
            self._tf.close()
        if self._fh is not None:
            self._fh.close()
        self._tf = None
        self._fh = None
        self._members = None

    def __enter__(self) -> "SampleStream":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ 迭代
    def __iter__(self) -> Iterator[Sample]:
        return self

    def __next__(self) -> Sample:
        """按成员流推进；key/shard 切换或 EOF 时强制校验当前样本完整性。"""
        while True:
            try:
                member, raw = self._next_member()
            except _ShardBoundary:
                # 一个 shard 的成员流结束：当前组装项若不完整立即失败，
                # 绝不把下一 shard（或下一 key）的部件补给它。
                if self._current is not None:
                    self._require_complete(self._current, where="<shard EOF>")
                    finished = self._current
                    self._current = None
                    return finished
                continue
            except StopIteration:
                if self._current is not None:
                    self._require_complete(self._current, where="<EOF>")
                    finished = self._current
                    self._current = None
                    return finished
                raise

            key, _part_name = self._parse_name(member)
            owner = self._shards[self._next_shard_pos]
            if self._current is not None and (
                key != self._current.key or owner.index != self._current.shard_index
            ):
                # key 切换：上一样本必须已经完整，否则缺件立即失败。
                self._require_complete(self._current, where=member.name)
                finished = self._current
                self._current = None
                self._begin_member(key, member, raw)
                return finished
            self._begin_member(key, member, raw)
            if len(self._current.parts) == len(self._part_set):
                finished = self._current
                self._current = None
                self._require_complete(finished, where=member.name)
                return finished

    def _next_member(self) -> tuple[tarfile.TarInfo, bytes]:
        """取下一个成员，惰性打开 shard；shard 边界抛 _ShardBoundary，全结束抛 StopIteration。"""
        while True:
            if self.exhausted:
                self._members = None
                raise StopIteration
            shard = self._shards[self._next_shard_pos]
            if self._tf is None and self._next_offset >= shard.path.stat().st_size:
                # 恢复点正好落在该 shard 文件尾：没有剩余成员，直接跨 shard。
                self._next_shard_pos += 1
                self._next_offset = 0
                self._stream_base = 0
                continue
            if self._tf is None:
                self._open_current_shard()
            assert self._members is not None
            try:
                member = next(self._members)
            except StopIteration:
                # 当前 shard 干净结束，推进到下一个 shard 的起点。
                self.close()
                self._next_shard_pos += 1
                self._next_offset = 0
                self._stream_base = 0
                raise _ShardBoundary
            raw = self._read_member(member)
            # 精确推进到下一个成员头（块对齐），不依赖带缓冲的 tell()。
            self._next_offset = self._next_header_offset(member)
            return member, raw

    def _open_current_shard(self) -> None:
        shard = self._shards[self._next_shard_pos]
        verify_shard_hash(shard)
        if not shard.path.is_file():
            raise DataAssemblyError(f"shard 文件不存在: {shard.path}")
        fh = shard.path.open("rb")
        try:
            if self._next_offset:
                # 从 checkpoint 保存的成员头偏移直接续读，前面的成员不会被重放。
                fh.seek(self._next_offset)
            self._stream_base = self._next_offset
            tf = tarfile.open(fileobj=fh, mode="r|")
        except (OSError, tarfile.TarError) as exc:
            fh.close()
            raise DataAssemblyError(
                f"打开 shard {shard.shard_id} 偏移 {self._next_offset} 失败: {exc}"
            ) from exc
        self._fh = fh
        self._tf = tf
        self._members = iter(tf)

    def _read_member(self, member: tarfile.TarInfo) -> bytes:
        shard = self._shards[self._next_shard_pos]
        if not member.isfile():
            raise DataAssemblyError(
                f"shard {shard.shard_id} 成员 {member.name!r} 不是普通文件"
            )
        if member.size > self._max_member_bytes:
            raise DataAssemblyError(
                f"shard {shard.shard_id} 成员 {member.name} 大小 {member.size} 超过上限 "
                f"{self._max_member_bytes}"
            )
        extractor = self._tf.extractfile(member) if self._tf is not None else None
        if extractor is None:
            raise DataAssemblyError(f"成员 {member.name} 无法读取内容")
        raw = extractor.read()
        if len(raw) != member.size:
            raise DataAssemblyError(
                f"成员 {member.name} 声明大小 {member.size} 实读 {len(raw)}"
            )
        return raw

    def _parse_name(self, member: tarfile.TarInfo) -> tuple[str, str]:
        shard = self._shards[self._next_shard_pos]
        name = member.name
        # USTAR 前缀 / 任何路径分隔都视为非法路径。
        if "/" in name or "\\" in name or name in (".", "..") or not name:
            raise DataAssemblyError(
                f"shard {shard.shard_id} 非法成员路径 {name!r}: 只允许 key.part"
            )
        match = _MEMBER_RE.fullmatch(name)
        if match is None:
            raise DataAssemblyError(
                f"shard {shard.shard_id} 非法成员名 {name!r}: 不匹配 key.part "
                f"(key 只含字母数字下划线)"
            )
        key, part_name = match.group(1), match.group(2)
        if part_name not in self._part_set:
            raise DataAssemblyError(
                f"shard {shard.shard_id} 成员 {name!r} 的部件 {part_name!r} "
                f"不在规定部件 {list(self._parts)} 中"
            )
        return key, part_name

    def _begin_member(self, key: str, member: tarfile.TarInfo, raw: bytes) -> None:
        shard = self._shards[self._next_shard_pos]
        if self._current is None:
            self._current = Sample(shard_index=shard.index, shard_id=shard.shard_id, key=key)
        current = self._current
        if current.key != key or current.shard_index != shard.index:
            # 防御性检查：正常流程在 __next__ 已处理切换。
            raise DataAssemblyError("内部错误：成员与当前组装样本不一致")
        part_name = member.name.rsplit(".", 1)[1]
        if part_name in current.parts:
            raise DataAssemblyError(
                f"shard {shard.shard_id} 样本 {key} 部件 {part_name} 重复 "
                f"(成员 {member.name}, 头偏移 {member.offset})"
            )
        current.parts[part_name] = Part(
            part=part_name,
            data=raw,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            header_offset=self._absolute_header_offset(member),
            data_offset=self._absolute_data_offset(member),
        )

    def _absolute_header_offset(self, member: tarfile.TarInfo) -> int:
        """流模式 TarInfo.offset 相对当前文件对象起点；seek 续读时需加回基准偏移。"""
        return self._stream_base + member.offset

    def _absolute_data_offset(self, member: tarfile.TarInfo) -> int:
        return self._absolute_header_offset(member) + _BLOCK

    def _next_header_offset(self, member: tarfile.TarInfo) -> int:
        """当前成员之后下一个 tar 头的绝对字节偏移。"""
        data_blocks = (member.size + _BLOCK - 1) // _BLOCK
        return self._stream_base + member.offset + _BLOCK + data_blocks * _BLOCK

    def _require_complete(self, sample: Sample, *, where: str) -> None:
        missing = [p for p in self._parts if p not in sample.parts]
        if missing:
            raise DataAssemblyError(
                f"shard {sample.shard_id} 样本 {sample.key} 在 {where} 处结束但缺少部件 "
                f"{missing}（规定部件 {list(self._parts)}）；不得用下一 key 部件补齐"
            )
        extra = [p for p in sample.parts if p not in self._part_set]
        if extra:  # 理论上不可达，部件集合在进入时已校验。
            raise DataAssemblyError(
                f"shard {sample.shard_id} 样本 {sample.key} 含未知部件 {extra}"
            )
