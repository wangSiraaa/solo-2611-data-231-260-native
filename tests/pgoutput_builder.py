"""测试辅助：独立拼装 pgoutput v1 消息字节与离线帧。

这里的编码器是专门为测试写的第二份实现，刻意不导入被测的 parser，
这样“编码 -> 解码”才能形成真正的交叉验证，而不是同一套代码自证。
"""

from __future__ import annotations

import struct
from typing import Iterable, Optional, Sequence, Tuple


def frame(message: bytes) -> bytes:
    """加 4 字节大端长度前缀（不含长度自身）。"""

    return struct.pack(">I", len(message)) + message


def cstring(text: str) -> bytes:
    return text.encode("utf-8") + b"\x00"


def begin(final_lsn: int, timestamp: int, xid: int) -> bytes:
    return b"B" + struct.pack(">QQI", final_lsn, timestamp, xid)


def commit(commit_lsn: int, end_lsn: int, timestamp: int, flags: int = 0) -> bytes:
    # Commit 布局：flags(1) + commit_lsn(Q) + end_lsn(Q) + timestamp(Q)。
    return b"C" + bytes([flags]) + struct.pack(">QQQ", commit_lsn, end_lsn, timestamp)


def relation(
    relid: int,
    namespace: str,
    name: str,
    columns: Sequence[Tuple[int, str, int, int]],
    replica_identity: str = "d",
) -> bytes:
    """columns: (flags, name, type_oid, typmod) 序列。"""

    payload = b"R" + struct.pack(">I", relid)
    payload += cstring(namespace) + cstring(name)
    payload += replica_identity.encode("ascii")
    payload += struct.pack(">H", len(columns))
    for flags, column_name, type_oid, typmod in columns:
        payload += bytes([flags]) + cstring(column_name)
        payload += struct.pack(">Ii", type_oid, typmod)
    return payload


def text_value(value: str) -> bytes:
    raw = value.encode("utf-8")
    return b"t" + struct.pack(">I", len(raw)) + raw


def null_value() -> bytes:
    return b"n"


def unchanged_value() -> bytes:
    return b"u"


def _tuple(values: Iterable[bytes]) -> bytes:
    values = list(values)
    return struct.pack(">H", len(values)) + b"".join(values)


def insert(relid: int, values: Sequence[bytes]) -> bytes:
    return b"I" + struct.pack(">I", relid) + b"N" + _tuple(values)


def update(
    relid: int,
    new_values: Sequence[bytes],
    old_key_values: Optional[Sequence[bytes]] = None,
) -> bytes:
    payload = b"U" + struct.pack(">I", relid)
    if old_key_values is not None:
        payload += b"K" + _tuple(old_key_values)
    payload += b"N" + _tuple(new_values)
    return payload


def delete(relid: int, key_values: Sequence[bytes]) -> bytes:
    return b"D" + struct.pack(">I", relid) + b"K" + _tuple(key_values)


def stream(messages: Iterable[bytes]) -> bytes:
    return b"".join(frame(m) for m in messages)


def expected_toast_body() -> str:
    """source.sql 中 string_agg(md5(g::text),'') 的独立重算（不执行 source.sql）。"""

    import hashlib

    return "".join(hashlib.md5(str(g).encode()).hexdigest() for g in range(1, 129))
