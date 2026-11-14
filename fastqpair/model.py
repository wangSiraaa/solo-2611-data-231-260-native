"""核心数据结构与配置对象。

全部按字节处理 FASTQ 内容；身份（read_id）也保留原始字节，
排序时按 UTF-8 字节序（即直接的字节序，对合法 UTF-8 文本等价）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 合法碱基：仅 ACGTN（大写）。质量：Phred+33 可打印 ASCII 33..126。
VALID_BASES = frozenset(b"ACGTN")
QUAL_MIN, QUAL_MAX = 33, 126

# 身份解析方案名称。
SCHEME_SUFFIX = "suffix"   # /name/1、/name/2 后缀
SCHEME_CASAVA = "casava"   # CASAVA 第二字段 1: / 2: mate 标识


class FatalTail(Exception):
    """文件级致命错误：后续边界无法确定，停止读取该文件并保留未读尾部。

    触发条件均是“无法确定后续边界”的情形：
      * 物理截断（头/序列/分隔/质量阶段遇到 EOF）；
      * 质量累计超过序列长度（超长质量，下一记录边界不可信）；
      * 结构行首字符非法（空行、缺失 + 分隔行等）。

    绝不能通过搜索下一个 ``@`` 行猜测恢复边界。
    """

    def __init__(
        self,
        reason: str,
        tail_offset: int,
        tail_length: int,
        preview: bytes,
        record_no: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.tail_offset = tail_offset          # 未读尾部起始的流字节偏移
        self.tail_length = tail_length          # 未读尾部字节数（压缩文件按解压流计）
        self.preview = preview                  # 未读尾部前缀（截断展示用）
        self.record_no = record_no              # 出错时正在解析的逻辑记录号


@dataclass(frozen=True)
class RawRecord:
    """解析器产出的原始记录：结构边界可确定，但字段合法性尚未裁定。"""

    record_no: int
    header: bytes          # 含开头 @ 的完整原始头（不含行尾）
    seq: bytes             # 多行包装已拼接为单行的原始序列字节
    qual: bytes            # 多行包装已拼接为单行的原始质量字节
    start_offset: int      # @ 头起始的流字节偏移（含整条记录的原始字节范围起点）
    end_offset: int        # 质量最后一行行尾之后的偏移（范围右端，exclusive）


@dataclass(frozen=True)
class ValidRecord:
    """通过全部字段与身份校验的合法记录。"""

    record_no: int
    read_id: bytes         # 规范身份（去掉 mate 声明后的名称），字节
    mate: int              # 1 或 2
    header: bytes          # 原始头
    seq: bytes
    qual: bytes
    start_offset: int
    end_offset: int
    source_index: int


@dataclass(frozen=True)
class QuarantineEntry:
    """隔离记录：边界可确定但字段/身份非法，保留其在来源中的位置。"""

    record_no: int
    source_index: int
    header: bytes
    reason: str
    read_id: bytes | None = None       # 若身份可解释则记录，否则 None
    declared_mate: int | None = None
    seq: bytes | None = None
    qual: bytes | None = None
    start_offset: int | None = None
    end_offset: int | None = None


@dataclass(frozen=True)
class SourceSpec:
    """一个来源文件的声明信息。"""

    path: str
    default_mate: int                  # 文件角色提供的缺省 mate（1 或 2）
    compression: str = "auto"          # auto / plain / gzip


@dataclass
class Config:
    """运行配置。兼容 fixtures/config.json 的描述性键名。"""

    pending_record_limit: int = 10000
    max_record_bytes: int = 64 * 1024 * 1024
    identity_schemes: tuple[str, ...] = (SCHEME_SUFFIX, SCHEME_CASAVA)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        # 描述性配置里的键名（pending_record_limit、max_record_bytes）直接兼容；
        # 布尔/数组形式的 identity_schemes 与 name_rules 文本都尽量宽松解释。
        kwargs: dict = {}
        if "pending_record_limit" in data:
            kwargs["pending_record_limit"] = int(data["pending_record_limit"])
        if "max_record_bytes" in data:
            kwargs["max_record_bytes"] = int(data["max_record_bytes"])

        schemes: list[str] = []
        if "identity_schemes" in data:
            raw = data["identity_schemes"]
            if isinstance(raw, str):
                raw = [raw]
            for item in raw:
                name = str(item).lower()
                if name in ("suffix", "/1", "/2", "slash"):
                    schemes.append(SCHEME_SUFFIX)
                elif name in ("casava", "second_token"):
                    schemes.append(SCHEME_CASAVA)
                else:
                    schemes.append(name)
        elif "name_rules" in data:
            # 从描述性 name_rules 文本推断启用的方案。
            text = str(data["name_rules"]).lower()
            if "/1" in text or "/2" in text or "suffix" in text:
                schemes.append(SCHEME_SUFFIX)
            if "second token" in text or "casava" in text or "1:" in text:
                schemes.append(SCHEME_CASAVA)
        if schemes:
            # 去重保序。
            kwargs["identity_schemes"] = tuple(dict.fromkeys(schemes))

        cfg = cls(**kwargs)
        if cfg.pending_record_limit < 1:
            raise ValueError("pending_record_limit 必须 >= 1（窗口不能为零）")
        for scheme in cfg.identity_schemes:
            if scheme not in (SCHEME_SUFFIX, SCHEME_CASAVA):
                raise ValueError(f"未知身份解析方案: {scheme}")
        return cfg


@dataclass
class SourceStatus:
    """单个来源文件的读取结论，写入报告与清单。"""

    source_index: int
    path: str
    compression: str                    # plain / gzip
    file_sha256: str
    default_mate: int
    logical_records: int = 0            # 边界完整产出的逻辑记录数（含隔离）
    valid_count: int = 0
    quarantined_count: int = 0
    fatal: FatalTail | None = None
    fatal_tail_offset: int | None = None
    fatal_tail_length: int | None = None
    fatal_preview_b64: str | None = None
    complete: bool = True               # False 表示该文件存在未读尾部
