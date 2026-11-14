"""FASTQ 流式解析。

设计约束（对应需求）：

* 普通文件与 gzip 均支持；gzip 记录的是**解压后 FASTQ 流**字节偏移，
  同时另存压缩原件 SHA256 与 compression=gzip，绝不冒充物理字节范围。
* 支持序列与质量的合法多行包装：序列读到以 ``+`` 开头的分隔行为止，
  质量按序列长度累计；质量行即使以 ``@`` 或 ``+`` 开头也只是质量。
* 不搜索下一个 ``@`` 猜测修复：截断（无法确定后续边界）与质量超长
  （累计质量超过序列长度，下一记录边界不可信）一律停止文件并报告未读尾部。
* 边界可确定但字段非法的记录由上层隔离，解析器只负责边界与原始字节。
"""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Iterator
from dataclasses import dataclass

from .model import FatalTail, RawRecord

GZIP_MAGIC = b"\x1f\x8b"
TAIL_PREVIEW = 200


@dataclass
class OpenedStream:
    binary: object                  # 已打开的二进制流（plain 或 gzip）
    compression: str                # "plain" / "gzip"
    file_sha256: str                # 磁盘原件 SHA256（gzip 时指压缩原件）


def open_stream(path: str, declared: str = "auto") -> OpenedStream:
    """按声明或魔数打开来源文件，并计算磁盘原件 SHA256。"""
    with open(path, "rb") as fh:
        head = fh.read(2)
        fh.seek(0)
        sha = hashlib.file_digest(fh, "sha256").hexdigest()

    is_gzip = head == GZIP_MAGIC
    if declared == "gzip" or (declared == "auto" and is_gzip):
        if not is_gzip:
            raise ValueError(f"{path}: 声明为 gzip 但缺少 gzip 魔数")
        return OpenedStream(gzip.open(path, "rb"), "gzip", sha)
    if declared == "gzip":
        raise ValueError(f"{path}: gzip 声明与内容不符")
    if is_gzip and declared == "plain":
        raise ValueError(f"{path}: 声明为普通文件但内容是 gzip")
    return OpenedStream(open(path, "rb"), "plain", sha)


class _LineStream:
    """逐行读取二进制流并维护绝对字节偏移。

    同时识别 ``\\n`` 与 ``\\r\\n`` 行尾；行内容不含行尾。偏移相对解压流。
    """

    def __init__(self, binary) -> None:
        self._fh = binary
        self.offset = 0

    def read_line(self) -> bytes | None:
        """读取下一行；EOF 返回 None，EOF 处的空内容不会伪造空行。"""
        line = self._fh.readline()
        if line == b"":
            return None
        self.offset += len(line)
        if line.endswith(b"\n"):
            line = line[:-1]
            if line.endswith(b"\r"):
                line = line[:-1]
        return line


def parse_fastq(stream: OpenedStream, max_record_bytes: int) -> Iterator[RawRecord]:
    """按严格边界逐条产出 :class:`RawRecord`。

    遇到无法确定后续边界的错误时抛出 :class:`FatalTail`，
    异常中的 tail_offset/tail_length 均按解压流字节计。
    """
    ls = _LineStream(stream.binary)
    record_no = 0

    while True:
        # ---- 阶段 1：@ 头 ----
        start = ls.offset
        header = ls.read_line()
        if header is None:
            return  # 干净 EOF
        if not header.startswith(b"@"):
            # 空行或缺失 @：结构已坏，无法信任后续边界，立即停止。
            # 注意：仅含 "@" 的行边界仍可确定，空身份交由身份层隔离而非致命停止。
            raise _fatal(ls, "记录头缺失或不是以 @ 开头", record_no + 1, start)
        record_no += 1

        # ---- 阶段 2：序列（可多行），读到 + 分隔行为止 ----
        seq_parts: list[bytes] = []
        while True:
            line = ls.read_line()
            if line is None:
                raise _fatal(ls, "序列阶段遇到 EOF（截断）", record_no, start)
            if line.startswith(b"+"):
                plus_line = line
                break
            if line == b"":
                raise _fatal(ls, "序列阶段出现空行（结构损坏）", record_no, start)
            seq_parts.append(line)
            if ls.offset - start > max_record_bytes:
                raise _fatal(ls, "单记录超出 max_record_bytes 限制", record_no, start)
        seq = b"".join(seq_parts)

        # ---- 阶段 3：质量（可多行），按序列字符数累计 ----
        # 质量行以 @ 或 + 开头也必须当作质量，绝不切分为下一记录。
        qual_parts: list[bytes] = []
        qual_len = 0
        target = len(seq)
        overflow_line: bytes | None = None
        while qual_len < target:
            line = ls.read_line()
            if line is None:
                raise _fatal(ls, "质量阶段遇到 EOF（截断）", record_no, start)
            if line == b"":
                raise _fatal(ls, "质量阶段出现空行（结构损坏）", record_no, start)
            qual_parts.append(line)
            qual_len += len(line)
            if ls.offset - start > max_record_bytes:
                raise _fatal(ls, "单记录超出 max_record_bytes 限制", record_no, start)
        if qual_len > target:
            # 超长质量：无法判断“多余部分”属于本记录还是下一记录，
            # 边界不可信，停止文件，不做任何 @ 搜索式修复。
            raise _fatal(ls, "质量字符数超过序列长度（超长质量）", record_no, start)
        qual = b"".join(qual_parts)
        end = ls.offset

        yield RawRecord(
            record_no=record_no,
            header=header,
            seq=seq,
            qual=qual,
            start_offset=start,
            end_offset=end,
        )


def _fatal(ls: _LineStream, reason: str, record_no: int, tail_offset: int) -> FatalTail:
    """构造致命尾部错误。tail 从出错位置起，读到流末尾以确定长度与预览。

    对 gzip 流，读到的是解压后剩余字节，长度与偏移口径一致。
    tail_offset 是出错记录起点（诊断锚点）；未读尾部仅指当前流位置之后
    尚未消费的字节，已读入行不重复计入 tail_length。
    """
    fh = ls._fh
    remaining = 0
    preview: bytes = b""
    while True:
        chunk = fh.read(65536)
        if not chunk:
            break
        remaining += len(chunk)
        if len(preview) < TAIL_PREVIEW:
            preview += chunk[: TAIL_PREVIEW - len(preview)]
    return FatalTail(
        reason=reason,
        tail_offset=tail_offset,
        tail_length=remaining,
        preview=preview[:TAIL_PREVIEW],
        record_no=record_no,
    )
