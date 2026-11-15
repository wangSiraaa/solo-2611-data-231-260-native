"""带消息序号与字节定位的异常体系。

所有解码/分帧错误都必须能回答两个问题：
* 第几条消息（0 基消息序号）；
* 在输入文件的哪个字节偏移出的问题。

这样 fixtures 里的 unknown-relation 与 truncated 两个负例才能给出
可机读、可定位的诊断，而不是一个含糊的 ValueError。
"""

from __future__ import annotations

from typing import Optional


class PgMirrorError(Exception):
    """所有本包异常的基类。"""

    def __init__(
        self,
        message: str,
        *,
        message_index: Optional[int] = None,
        frame_offset: Optional[int] = None,
        byte_offset: Optional[int] = None,
    ) -> None:
        self.message_index = message_index
        self.frame_offset = frame_offset
        self.byte_offset = byte_offset
        located = message
        if message_index is not None or frame_offset is not None or byte_offset is not None:
            loc = []
            if message_index is not None:
                loc.append(f"message_index={message_index}")
            if frame_offset is not None:
                loc.append(f"frame_offset={frame_offset}")
            if byte_offset is not None:
                loc.append(f"byte_offset={byte_offset}")
            located = f"{message} ({', '.join(loc)})"
        super().__init__(located)


class FramingError(PgMirrorError):
    """外层 4 字节大端长度分帧本身不合法（例如长度头被截断）。"""


class TruncatedFrameError(FramingError):
    """帧头声明的长度超过文件剩余字节。"""

    def __init__(
        self,
        *,
        message_index: int,
        frame_offset: int,
        declared_length: int,
        available: int,
    ) -> None:
        self.declared_length = declared_length
        self.available = available
        super().__init__(
            "truncated frame: declared length %d but only %d payload bytes remain "
            "(missing %d)" % (declared_length, available, declared_length - available),
            message_index=message_index,
            frame_offset=frame_offset,
            byte_offset=frame_offset + 4,
        )


class ProtocolError(PgMirrorError):
    """pgoutput 消息内容违反协议或本工具声明支持的范围。"""


class MalformedMessageError(ProtocolError):
    """消息字节结构损坏：长度不符、字段越界、非法标记等。"""


class UnsupportedFeatureError(ProtocolError):
    """协议本身能表达，但本工具明确不支持的特性。

    包括 O/full-old 复本标识、binary 元组、TRUNCATE、streaming 事务、
    类型扩展，以及“尾部追加 nullable 列”之外的任何 schema 变更。
    """


class UnknownRelationError(ProtocolError):
    """DML 引用了当前事务及其已提交历史中从未出现过的 relid。"""

    def __init__(self, *, relid: int, message_index: int, frame_offset: int) -> None:
        self.relid = relid
        super().__init__(
            f"DML references unknown relation relid={relid} (uint32); "
            "no Relation message has defined it",
            message_index=message_index,
            frame_offset=frame_offset,
            byte_offset=frame_offset + 1,
        )


class TransactionAbortedError(PgMirrorError):
    """事务中途出现任何协议/支持范围错误，整个事务不得发布。

    包装原始原因，驱动层据此把该事务标记为 aborted 并保留已提交前缀。
    """

    def __init__(self, cause: Exception, *, message_index: int, xid: Optional[int]) -> None:
        self.cause = cause
        self.xid = xid
        super().__init__(
            f"transaction{xid if xid is not None else ''} aborted and will not be "
            f"published: {cause}",
            message_index=message_index,
        )
