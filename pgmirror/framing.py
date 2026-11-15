"""离线分帧层。

fixtures 的存储格式不是复制协议的 wire envelope，而只是：

    uint32 BE length（不含长度自身）4 字节） + 一条完整 pgoutput 消息

本模块只负责把字节流切成 (消息序号, 帧偏移, 消息原字节)，绝不解释
消息语义，消息原字节保持 pgoutput 原样上交协议层。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Iterator

from .errors import FramingError, TruncatedFrameError

_LENGTH_HEAD = struct.Struct(">I")


@dataclass(frozen=True)
class Frame:
    """一条离线帧的定位信息与原始 pgoutput 消息字节。"""

    index: int
    frame_offset: int
    payload: bytes

    @property
    def message_type(self) -> str:
        return chr(self.payload[0]) if self.payload else "?"


def iter_frames(data: bytes) -> Iterator[Frame]:
    """按 4 字节大端长度依次切帧。

    长度头本身被截断、或声明长度越过 EOF 都抛出带序号/字节偏移的
    FramingError；调用方可以据此区分“干净 EOF”与“半截消息”。
    """

    total = len(data)
    offset = 0
    index = 0
    while offset < total:
        frame_offset = offset
        if total - offset < _LENGTH_HEAD.size:
            raise FramingError(
                "truncated length header: need 4 bytes but only %d remain"
                % (total - offset),
                message_index=index,
                frame_offset=frame_offset,
                byte_offset=offset,
            )
        (declared_length,) = _LENGTH_HEAD.unpack_from(data, offset)
        payload_start = offset + _LENGTH_HEAD.size
        payload_end = payload_start + declared_length
        if payload_end > total:
            raise TruncatedFrameError(
                message_index=index,
                frame_offset=frame_offset,
                declared_length=declared_length,
                available=total - payload_start,
            )
        if declared_length == 0:
            raise FramingError(
                "zero-length pgoutput message is not valid",
                message_index=index,
                frame_offset=frame_offset,
                byte_offset=payload_start,
            )
        yield Frame(index=index, frame_offset=frame_offset, payload=data[payload_start:payload_end])
        offset = payload_end
        index += 1
