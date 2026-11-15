"""pgoutput proto_version=1 消息的纯字节解析器。

本模块只做一件事：把一条完整 pgoutput 消息原字节解释成结构化 dataclass。
它不碰 SQLite、不碰事务状态，因此可以被单元测试独立覆盖。

支持的消息类型（题面范围）：
    B Begin / C Commit / R Relation / I Insert / U Update / D Delete
明确拒绝：
    O origin、Y/L/S 等 streaming 消息、T TRUNCATE、binary('b') 元组、
    非默认 replica identity 的 full-old 元组、不支持的类型 OID。

字段布局依据 PostgreSQL 16 pgoutput 消息格式，并以 fixtures 的真实字节
逐字段核对过（Relation 头为 5 字节，relreplident 在命名空间之后，
列计数为 uint16，列内 typmod 为 int32）。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .errors import MalformedMessageError, UnsupportedFeatureError

# 关系消息里列属性标记位（pg_attribute.attflags 镜像）。
COL_FLAG_KEY = 0x0001  # 列属于复制身份（默认 identity 下即主键）

# tuple 数据列标记。
TUPLE_NULL = "n"       # 真 NULL
TUPLE_UNCHANGED = "u"  # unchanged TOAST，值未随消息发出
TUPLE_TEXT = "t"       # 文本格式、uint32 长度前缀
_TUPLE_MARKERS = frozenset((TUPLE_NULL, TUPLE_UNCHANGED, TUPLE_TEXT))

# 整条 tuple 的可选前缀。
TUPLE_PRESENT_NEW = "N"   # 普通新 tuple（Insert 恒为 N）
TUPLE_PRESENT_OLD_KEY = "K"  # 仅含旧键的 old tuple（默认 identity 的 Update/Delete）


class _Reader:
    """带越界保护的顺序读取器，任何越界都转成带定位的 MalformedMessageError。"""

    def __init__(self, payload: bytes, message_index: int, frame_offset: int) -> None:
        self._buf = payload
        self._len = len(payload)
        # payload[0] 是消息类型字节，已由 parse_message 消费，解析器从 1 开始。
        self._pos = 1
        self.message_index = message_index
        self.frame_offset = frame_offset

    @property
    def pos(self) -> int:
        return self._pos

    @property
    def remaining(self) -> int:
        return self._len - self._pos

    def byte(self) -> int:
        if self._pos >= self._len:
            self._fail("unexpected end of message while reading 1 byte")
        value = self._buf[self._pos]
        self._pos += 1
        return value

    def marker(self) -> str:
        return chr(self.byte())

    def uint8(self) -> int:
        return self.byte()

    def uint16(self) -> int:
        return self._unpack(2, ">H")

    def uint32(self) -> int:
        return self._unpack(4, ">I")

    def int32(self) -> int:
        return self._unpack(4, ">i")

    def uint64(self) -> int:
        return self._unpack(8, ">Q")

    def cstring(self) -> str:
        end = self._buf.find(b"\x00", self._pos)
        if end < 0:
            self._fail("unterminated null-terminated string")
        raw = self._buf[self._pos:end]
        self._pos = end + 1
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self._fail(f"invalid UTF-8 in identifier: {exc}")

    def take(self, size: int) -> bytes:
        if size < 0 or self._pos + size > self._len:
            self._fail(f"need {size} bytes but only {self.remaining} remain")
        chunk = self._buf[self._pos:self._pos + size]
        self._pos += size
        return chunk

    def expect_end(self, declared_length: int) -> None:
        if self._pos != self._len:
            self._fail(
                f"message has {self._len - self._pos} trailing bytes "
                f"(declared length={declared_length})"
            )

    def _unpack(self, size: int, fmt: str) -> int:
        if self._pos + size > self._len:
            self._fail(f"need {size} bytes for {fmt} but only {self.remaining} remain")
        (value,) = struct.unpack_from(fmt, self._buf, self._pos)
        self._pos += size
        return value

    def _fail(self, reason: str) -> None:
        raise MalformedMessageError(
            reason,
            message_index=self.message_index,
            frame_offset=self.frame_offset,
            byte_offset=self.frame_offset + self._pos,
        )


@dataclass(frozen=True)
class BeginMessage:
    final_lsn: int          # 事务提交点 LSN（与随后 Commit 的 commit_lsn 相同）
    commit_timestamp: int   # 微秒级时间戳，原样保留
    xid: int


@dataclass(frozen=True)
class CommitMessage:
    flags: int
    commit_lsn: int
    end_lsn: int
    commit_timestamp: int


@dataclass(frozen=True)
class ColumnSpec:
    flags: int
    name: str
    type_oid: int
    typmod: int

    @property
    def is_key(self) -> bool:
        return bool(self.flags & COL_FLAG_KEY)


@dataclass(frozen=True)
class RelationMessage:
    relid: int
    namespace: str
    name: str
    replica_identity: str
    columns: Tuple[ColumnSpec, ...]


@dataclass(frozen=True)
class TupleValue:
    """tuple 中一列的原始形态。text 仍是字节，类型解释交给引擎层。"""

    marker: str                 # 'n' | 'u' | 't'
    raw: Optional[bytes] = None  # 仅 marker == 't' 时有值


@dataclass(frozen=True)
class InsertMessage:
    relid: int
    new_tuple: Tuple[TupleValue, ...]


@dataclass(frozen=True)
class UpdateMessage:
    relid: int
    old_key: Optional[Tuple[TupleValue, ...]]  # marker == 'K' 时存在
    new_tuple: Tuple[TupleValue, ...]


@dataclass(frozen=True)
class DeleteMessage:
    relid: int
    key_tuple: Tuple[TupleValue, ...]          # 默认 identity 下恒为 K tuple


_PARSERS = {}


def _register(message_type: str):
    def deco(func):
        _PARSERS[message_type] = func
        return func

    return deco


def parse_message(payload: bytes, message_index: int, frame_offset: int):
    """解析单条 pgoutput 消息，返回对应 dataclass。"""

    if not payload:
        raise MalformedMessageError(
            "empty pgoutput message",
            message_index=message_index,
            frame_offset=frame_offset,
            byte_offset=frame_offset,
        )
    message_type = chr(payload[0])
    parser = _PARSERS.get(message_type)
    if parser is None:
        if message_type == "T":
            raise UnsupportedFeatureError(
                "TRUNCATE messages are not supported",
                message_index=message_index,
                frame_offset=frame_offset,
                byte_offset=frame_offset,
            )
        if message_type in ("O",):
            raise UnsupportedFeatureError(
                "ORIGIN messages are not supported",
                message_index=message_index,
                frame_offset=frame_offset,
                byte_offset=frame_offset,
            )
        if message_type in ("Y", "L"):  # Stream Start / Stream Stop
            raise UnsupportedFeatureError(
                f"streaming transaction message {message_type!r} is not supported",
                message_index=message_index,
                frame_offset=frame_offset,
                byte_offset=frame_offset,
            )
        if message_type == "S":  # Stream Commit
            raise UnsupportedFeatureError(
                "streaming Stream Commit message is not supported",
                message_index=message_index,
                frame_offset=frame_offset,
                byte_offset=frame_offset,
            )
        raise UnsupportedFeatureError(
            f"unsupported pgoutput message type {message_type!r}",
            message_index=message_index,
            frame_offset=frame_offset,
            byte_offset=frame_offset,
        )
    reader = _Reader(payload, message_index, frame_offset)
    parsed = parser(reader)
    reader.expect_end(len(payload))
    return message_type, parsed


@_register("B")
def _parse_begin(reader: _Reader) -> BeginMessage:
    final_lsn = reader.uint64()
    commit_timestamp = reader.uint64()
    xid = reader.uint32()
    return BeginMessage(
        final_lsn=final_lsn,
        commit_timestamp=commit_timestamp,
        xid=xid,
    )


@_register("C")
def _parse_commit(reader: _Reader) -> CommitMessage:
    flags = reader.uint8()
    commit_lsn = reader.uint64()
    end_lsn = reader.uint64()
    commit_timestamp = reader.uint64()
    return CommitMessage(
        flags=flags,
        commit_lsn=commit_lsn,
        end_lsn=end_lsn,
        commit_timestamp=commit_timestamp,
    )


@_register("R")
def _parse_relation(reader: _Reader) -> RelationMessage:
    relid = reader.uint32()
    namespace = reader.cstring()
    name = reader.cstring()
    replica_identity = reader.marker()
    if replica_identity not in ("d", "i", "n", "f"):
        reader._fail(f"invalid replica identity setting {replica_identity!r}")
    if replica_identity == "f":
        raise UnsupportedFeatureError(
            "REPLICA IDENTITY FULL (old tuple marker 'O') is not supported",
            message_index=reader.message_index,
            frame_offset=reader.frame_offset,
            byte_offset=reader.frame_offset + reader.pos,
        )
    column_count = reader.uint16()
    if column_count == 0:
        reader._fail("relation declares zero columns")
    columns: List[ColumnSpec] = []
    for _ in range(column_count):
        flags = reader.uint8()
        column_name = reader.cstring()
        type_oid = reader.uint32()
        typmod = reader.int32()
        columns.append(
            ColumnSpec(flags=flags, name=column_name, type_oid=type_oid, typmod=typmod)
        )
    return RelationMessage(
        relid=relid,
        namespace=namespace,
        name=name,
        replica_identity=replica_identity,
        columns=tuple(columns),
    )


def _parse_tuple(reader: _Reader) -> Tuple[TupleValue, ...]:
    column_count = reader.uint16()
    values: List[TupleValue] = []
    for _ in range(column_count):
        marker = reader.marker()
        if marker not in _TUPLE_MARKERS:
            reader._fail(
                f"invalid tuple column marker {marker!r}; expected one of n/u/t"
            )
        if marker == TUPLE_TEXT:
            length = reader.uint32()
            raw = reader.take(length)
            values.append(TupleValue(marker=marker, raw=raw))
        else:
            values.append(TupleValue(marker=marker))
    return tuple(values)


def _parse_new_tuple_marker(reader: _Reader) -> None:
    marker = reader.marker()
    if marker == "b":
        raise UnsupportedFeatureError(
            "binary tuple format ('b') is not supported; only text tuples are",
            message_index=reader.message_index,
            frame_offset=reader.frame_offset,
            byte_offset=reader.frame_offset + reader.pos,
        )
    if marker != TUPLE_PRESENT_NEW:
        reader._fail(f"expected new-tuple marker 'N', got {marker!r}")


@_register("I")
def _parse_insert(reader: _Reader) -> InsertMessage:
    relid = reader.uint32()
    _parse_new_tuple_marker(reader)
    new_tuple = _parse_tuple(reader)
    return InsertMessage(relid=relid, new_tuple=tuple(new_tuple))


@_register("U")
def _parse_update(reader: _Reader) -> UpdateMessage:
    relid = reader.uint32()
    lead_marker = reader.marker()
    old_key = None
    if lead_marker == TUPLE_PRESENT_OLD_KEY:
        old_key = _parse_tuple(reader)
        # K old tuple 之后，新 tuple 仍带自己的 N/b 标记。
        _parse_new_tuple_marker(reader)
    elif lead_marker == TUPLE_PRESENT_NEW:
        pass  # 没有 old tuple，后面直接是新 tuple。
    elif lead_marker == "O":
        raise UnsupportedFeatureError(
            "UPDATE with full-old-tuple marker 'O' (REPLICA IDENTITY FULL) is not supported",
            message_index=reader.message_index,
            frame_offset=reader.frame_offset,
            byte_offset=reader.frame_offset + reader.pos,
        )
    elif lead_marker == "b":
        raise UnsupportedFeatureError(
            "binary tuple format ('b') is not supported",
            message_index=reader.message_index,
            frame_offset=reader.frame_offset,
            byte_offset=reader.frame_offset + reader.pos,
        )
    else:
        reader._fail(f"unexpected update tuple marker {lead_marker!r}")
    new_tuple = _parse_tuple(reader)
    return UpdateMessage(relid=relid, old_key=old_key, new_tuple=tuple(new_tuple))


@_register("D")
def _parse_delete(reader: _Reader) -> DeleteMessage:
    relid = reader.uint32()
    marker = reader.marker()
    if marker == "O":
        raise UnsupportedFeatureError(
            "DELETE with full-old-tuple marker 'O' (REPLICA IDENTITY FULL) is not supported",
            message_index=reader.message_index,
            frame_offset=reader.frame_offset,
            byte_offset=reader.frame_offset + reader.pos,
        )
    if marker == "b":
        raise UnsupportedFeatureError(
            "binary tuple format ('b') is not supported",
            message_index=reader.message_index,
            frame_offset=reader.frame_offset,
            byte_offset=reader.frame_offset + reader.pos,
        )
    if marker != TUPLE_PRESENT_OLD_KEY:
        reader._fail(f"DELETE expected old-key marker 'K', got {marker!r}")
    key_tuple = _parse_tuple(reader)
    return DeleteMessage(relid=relid, key_tuple=key_tuple)
