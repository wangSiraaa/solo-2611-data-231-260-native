"""复合业务键的字节序编解码（与 SQLite BLOB 二进制排序对齐）。

两阶段布局（详见 docs/LAYOUT.md）：

1. 升序空间布局（字段自定界，按 schema 顺序直接拼接，无额外分隔符）：
   * 字段首字节是标签：0x00=NULL，0x02=非空（0x01 保留为转义引导字节）。
   * int64 非空 = 0x02 + 8 字节大端；text/bytes 非空 = 0x02 + 转义负载 + 0x00 终止符。
   * 转义：负载中的 0x00 -> 0x01 0x01，0x01 -> 0x01 0x02；终止符 0x00 只可能
     出现在字段边界，字段可无歧义切分。
   * 该布局下 BLOB 字典序 == 所有列均按 NULLS FIRST 的升序业务序。

2. 按列声明做逐字节变换（只作用于本字段段，解码时逐列可逆）：
   * order=asc  + nulls first：恒等；
   * order=asc  + nulls last ：仅在字段首字节交换标签 0x00<->0x02（NULL 沉尾）；
   * order=desc + nulls last ：整段按位取反（字典序整体反转，NULL 落尾）；
   * order=desc + nulls first：取反后，仅在首字节交换 0xFD<->0xFF（NULL 提头）。

头部交换绝不触碰负载（负载允许出现任意字节，含 0x02），终止符也不参与头部
交换，其最终值仅取决于 order（asc=0x00，desc=0xFF）。每列的字节序被独立
保持或整体反转，因此拼接后的 BLOB 字典序精确等价于逐列声明的业务序。
"""

from __future__ import annotations

from typing import Any, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

# 升序空间下的标签与转义字节。取值刻意拉开距离，交换/取反后仍可明确识别。
_TAG_NULL = 0x00
_TAG_VALUE = 0x02
_ESC = 0x01      # 转义引导字节
_END = 0x00      # 变长字段终止符
# 转义对：负载中的 0x00 -> 0x01 0x01；负载中的 0x01 -> 0x01 0x02。
_ESC_ZERO = 0x01
_ESC_ONE = 0x02

_ALLOWED_TYPES = ("int64", "text", "bytes")
_ALLOWED_ORDERS = ("asc", "desc")
_ALLOWED_NULLS = ("first", "last")
_HEX_CHARS = set(b"0123456789abcdef")


# ---------------------------------------------------------------------------
# schema 与取值校验
# ---------------------------------------------------------------------------

def validate_schema(schema: list[dict]) -> None:
    """检查 schema.json 结构是否合法，非法时抛 ValueError。"""
    if not isinstance(schema, list) or not schema:
        raise ValueError("schema 必须是非空 list")
    seen: set[str] = set()
    for i, col in enumerate(schema):
        if not isinstance(col, dict):
            raise ValueError(f"第 {i} 列不是对象: {col!r}")
        name = col.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"第 {i} 列缺少有效 name")
        if name in seen:
            raise ValueError(f"列名重复: {name}")
        seen.add(name)
        if col.get("type") not in _ALLOWED_TYPES:
            raise ValueError(
                f"列 {name} 类型未知: {col.get('type')!r}，"
                f"仅支持 {_ALLOWED_TYPES}"
            )
        if col.get("order") not in _ALLOWED_ORDERS:
            raise ValueError(f"列 {name} 的 order 非法: {col.get('order')!r}")
        if col.get("nulls") not in _ALLOWED_NULLS:
            raise ValueError(f"列 {name} 的 nulls 非法: {col.get('nulls')!r}")


def _normalize_value(col: dict, value: Any) -> Any:
    """把 JSON/外部取值规整为内部表示并做范围/格式校验。"""
    ctype = col["type"]
    if value is None:
        return None
    name = col["name"]

    if ctype == "int64":
        # bool 是 int 的子类，业务上必须显式拒绝。
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"列 {name} 需要 int64，得到 {type(value).__name__}")
        if not INT64_MIN <= value <= INT64_MAX:
            raise ValueError(f"列 {name} 整数超出 int64 范围: {value}")
        return value

    if ctype == "text":
        if not isinstance(value, str):
            raise ValueError(f"列 {name} 需要 text(str)，得到 {type(value).__name__}")
        # 严格 UTF-8：lone surrogate 无法编码，直接拒绝。
        value.encode("utf-8")
        return value

    # bytes：外部表示固定为 {"hex": "..."}，仅接受偶数位小写 hex，空串合法。
    if not isinstance(value, dict) or set(value.keys()) != {"hex"}:
        raise ValueError(f"列 {name} 的 bytes 必须是 {{\"hex\": \"...\"}} 形式")
    hex_str = value["hex"]
    if not isinstance(hex_str, str) or len(hex_str) % 2:
        raise ValueError(f"列 {name} hex 必须是偶数位字符串")
    raw = hex_str.encode("ascii", errors="ignore")
    if len(raw) != len(hex_str) or any(ch not in _HEX_CHARS for ch in raw):
        raise ValueError(f"列 {name} hex 含非小写十六进制字符: {hex_str!r}")
    return bytes.fromhex(hex_str)


# ---------------------------------------------------------------------------
# 原语
# ---------------------------------------------------------------------------

def _invert(data: bytes) -> bytes:
    return bytes(b ^ 0xFF for b in data)


def _swap_head_pair(data: bytes, a: int, b: int) -> bytes:
    """仅在字段首字节上交换标签 a <-> b；负载字节一律不触碰。

    负载中允许出现任意字节（只转义了 0x00/0x01），标签交换绝不能作用于整段，
    否则会破坏负载并使解码错位。
    """
    if not data:
        return data
    head = data[0]
    if head == a:
        head = b
    elif head == b:
        head = a
    return bytes([head]) + data[1:]


def _escape_payload(raw: bytes) -> bytes:
    out = bytearray()
    for b in raw:
        if b == 0x00:
            out.append(_ESC)
            out.append(_ESC_ZERO)
        elif b == _ESC:
            out.append(_ESC)
            out.append(_ESC_ONE)
        else:
            out.append(b)
    return bytes(out)


def _unescape_payload(raw: bytes, col_name: str) -> bytes:
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        b = raw[i]
        if b != _ESC:
            out.append(b)
            i += 1
            continue
        if i + 1 >= n:
            raise ValueError(f"列 {col_name} 解码遇到悬空转义字节")
        nxt = raw[i + 1]
        if nxt == _ESC_ZERO:
            out.append(0x00)
        elif nxt == _ESC_ONE:
            out.append(_ESC)
        else:
            raise ValueError(f"列 {col_name} 解码遇到非法转义对 0x01 0x{nxt:02x}")
        i += 2
    return bytes(out)


def _encode_int64(value: int) -> bytes:
    # 翻转符号位后，有符号 int64 的数值序 == uint64 的字节序。
    return (value + (1 << 63)).to_bytes(8, "big", signed=False)


def _decode_int64(raw: bytes) -> int:
    return int.from_bytes(raw, "big", signed=False) - (1 << 63)


# ---------------------------------------------------------------------------
# 升序空间的单字段编码（标签恒为 NULL=0x00 / VALUE=0x02）
# ---------------------------------------------------------------------------

def _encode_field_asc(col: dict, value: Any) -> bytes:
    value = _normalize_value(col, value)
    if value is None:
        return bytes([_TAG_NULL])
    if col["type"] == "int64":
        return bytes([_TAG_VALUE]) + _encode_int64(value)
    if col["type"] == "text":
        raw = value.encode("utf-8")
    else:  # bytes
        raw = value
    return bytes([_TAG_VALUE]) + _escape_payload(raw) + bytes([_END])


def _apply_order(col: dict, segment: bytes) -> bytes:
    """把升序空间（NULL 标签 0x00 / 值标签 0x02）字段段变换为最终字节段。"""
    if col["order"] == "asc":
        if col["nulls"] == "first":
            return segment                       # 恒等：0x00 < 0x02，NULL 在前
        # 头部交换标签：NULL->0x02、值->0x00，NULL 沉到值之后。
        return _swap_head_pair(segment, _TAG_NULL, _TAG_VALUE)
    # desc：整段取反，字典序整体反转（终止符同样参与）。
    segment = _invert(segment)                   # NULL->0xFF，值头->0xFD
    if col["nulls"] == "last":
        return segment                           # 0xFF > 0xFD，NULL 在末尾
    # 头部交换取反后的标签：NULL->0xFD、值头->0xFF，NULL 提回头部。
    return _swap_head_pair(segment, 0xFD, 0xFF)


def _undo_order(col: dict, segment: bytes) -> bytes:
    """_apply_order 的逆变换（同样只作用于字段首字节）。"""
    if col["order"] == "asc":
        if col["nulls"] == "first":
            return segment
        return _swap_head_pair(segment, _TAG_NULL, _TAG_VALUE)
    if col["nulls"] == "first":
        segment = _swap_head_pair(segment, 0xFD, 0xFF)
    return _invert(segment)


# ---------------------------------------------------------------------------
# 单字段编码（对外保留：单列 schema 时等价于 encode；供测试/工具使用）
# ---------------------------------------------------------------------------

def encode_field(col: dict, value: Any) -> bytes:
    """编码单个字段，返回可直接拼接的自定界字节段。"""
    validate_schema([col])
    return _apply_order(col, _encode_field_asc(col, value))


# ---------------------------------------------------------------------------
# 复合键编码
# ---------------------------------------------------------------------------

def encode(schema: list[dict], values: dict[str, Any]) -> bytes:
    """把业务键 dict 编码为一条复合 BLOB。"""
    validate_schema(schema)
    if not isinstance(values, dict):
        raise ValueError("encode 的 values 必须是 dict")
    parts: list[bytes] = []
    for col in schema:
        if col["name"] not in values:
            raise ValueError(f"缺少列 {col['name']} 的取值")
        segment = _encode_field_asc(col, values[col["name"]])
        parts.append(_apply_order(col, segment))
    return b"".join(parts)


# ---------------------------------------------------------------------------
# 解码
# ---------------------------------------------------------------------------

def _read_field(buf: bytes, pos: int, col: dict) -> tuple[Any, int]:
    """从 buf[pos:] 读取一个最终空间字段，返回 (业务值, 下一字段起始位置)。"""
    if pos >= len(buf):
        raise ValueError(f"列 {col['name']} 解码时数据截断")

    if col["type"] == "int64":
        # NULL 是单字节标签；先按列变换逆推判断，避免把 desc 取反后的 0xFD 误判。
        head = _undo_order(col, bytes([buf[pos]]))
        if head == bytes([_TAG_NULL]):
            return None, pos + 1
        width = 9  # 标签 + 8 字节定长
        if pos + width > len(buf):
            raise ValueError(f"列 {col['name']} int64 字段截断")
        segment = _undo_order(col, buf[pos:pos + width])
        if segment[0] != _TAG_VALUE:
            raise ValueError(f"列 {col['name']} int64 标签非法: 0x{buf[pos]:02x}")
        return _decode_int64(segment[1:]), pos + width

    # 变长 text/bytes：NULL 是单字节标签（无终止符），先识别。
    if _undo_order(col, bytes([buf[pos]])) == bytes([_TAG_NULL]):
        return None, pos + 1

    # 非空：终止符只经历取反（头部交换不作用于它），故最终值仅取决于 order。
    end_in_final = 0xFF if col["order"] == "desc" else _END
    end = buf.find(end_in_final, pos + 1)
    if end < 0:
        raise ValueError(f"列 {col['name']} 缺少字段终止符")
    segment = _undo_order(col, buf[pos:end + 1])
    if segment[0] != _TAG_VALUE:
        raise ValueError(f"列 {col['name']} 标签非法: 0x{buf[pos]:02x}")
    raw = _unescape_payload(segment[1:-1], col["name"])
    if col["type"] == "text":
        return raw.decode("utf-8"), end + 1
    return raw, end + 1


def decode(schema: list[dict], blob: bytes) -> dict[str, Any]:
    """把复合 BLOB 还原为业务键 dict（bytes 列回到 {"hex": ...} 形式）。"""
    validate_schema(schema)
    if not isinstance(blob, (bytes, bytearray, memoryview)):
        raise ValueError("decode 输入必须是 bytes")
    buf = bytes(blob)
    out: dict[str, Any] = {}
    pos = 0
    for col in schema:
        value, pos = _read_field(buf, pos, col)
        if col["type"] == "bytes" and value is not None:
            value = {"hex": value.hex()}
        out[col["name"]] = value
    if pos != len(buf):
        raise ValueError(f"解码后仍有 {len(buf) - pos} 个多余字节")
    return out


# ---------------------------------------------------------------------------
# 前缀查询 -> BLOB 区间
# ---------------------------------------------------------------------------

def _successor(prefix: bytes) -> Optional[bytes]:
    """最小的、严格大于所有以 prefix 为前缀的字节串的 BLOB。

    若不存在这样的有限后继（prefix 为空或全为 0xFF），返回 None 表示无穷上界。
    """
    data = bytearray(prefix)
    for i in range(len(data) - 1, -1, -1):
        if data[i] != 0xFF:
            data[i] += 1
            return bytes(data[:i + 1])
    return None


def _field_prefix(col: dict, value: Any, full: bool) -> bytes:
    """生成单列前缀在最终字节序空间中的字节段。

    full=True  -> 完整等值字段（变长列带终止符）。
    full=False -> 仅 text starts_with 的原始前缀（无终止符）。
    """
    value = _normalize_value(col, value)
    if value is None:
        if not full:
            raise ValueError("starts_with 不能作用于 NULL")
        return _apply_order(col, bytes([_TAG_NULL]))

    if col["type"] == "int64":
        segment = bytes([_TAG_VALUE]) + _encode_int64(value)
        return _apply_order(col, segment)

    if col["type"] == "text":
        raw = value.encode("utf-8")
        segment = bytes([_TAG_VALUE]) + _escape_payload(raw)
        if full:
            segment += bytes([_END])
        return _apply_order(col, segment)

    # bytes 不支持 starts_with
    if not full:
        raise ValueError("starts_with 仅支持 text 列")
    segment = bytes([_TAG_VALUE]) + _escape_payload(value) + bytes([_END])
    return _apply_order(col, segment)


def compile_prefix(
    schema: list[dict],
    equals_prefix: list[Any],
    starts_with: Optional[str] = None,
) -> tuple[bytes, Optional[bytes]]:
    """把“前 k 列等值 + 紧接 text 列 starts_with”编译为 BLOB 半开区间。

    返回 (下界, 上界)：下界恒包含；上界为 None 表示无穷上界（SQL 不写上界），
    否则为排他上界。满足条件的键在最终字节序空间中恰好共享同一前缀，且字段
    变换保持前缀关系（取反为双射、标签交换为双射），因此无论 asc/desc、何种
    NULL 位置，统一为 [prefix, successor(prefix))。
    """
    validate_schema(schema)
    if not isinstance(equals_prefix, list):
        raise ValueError("equals_prefix 必须是 list")
    k = len(equals_prefix)
    if k > len(schema):
        raise ValueError("等值前缀列数超过 schema 列数")

    parts: list[bytes] = []
    for i, value in enumerate(equals_prefix):
        parts.append(_field_prefix(schema[i], value, full=True))

    if starts_with is not None:
        if k >= len(schema):
            raise ValueError("starts_with 后没有可用的 text 列")
        target = schema[k]
        if target["type"] != "text":
            raise ValueError(
                f"starts_with 仅支持 text 列，列 {target['name']} 是 {target['type']}"
            )
        if not isinstance(starts_with, str):
            raise ValueError("starts_with 必须是 str")
        starts_with.encode("utf-8")
        # 关键：文本前缀不是完整字段，不能追加终止符。
        parts.append(_field_prefix(target, starts_with, full=False))

    lower = b"".join(parts)
    upper = _successor(lower)
    return lower, upper


# ---------------------------------------------------------------------------
# 残余核对（区间取数后在 Python 侧做精确判断，防止前缀边界理解错误）
# ---------------------------------------------------------------------------

def matches(
    schema: list[dict],
    values: dict[str, Any],
    equals_prefix: list[Any],
    starts_with: Optional[str] = None,
) -> bool:
    """判断已解码的业务键是否满足前缀条件（区间扫描后的残余核对）。"""
    k = len(equals_prefix)
    for i, want in enumerate(equals_prefix):
        col = schema[i]
        got = values[col["name"]]
        if col["type"] == "bytes":
            # 解码侧 bytes 列统一为 {"hex": ...}，先把规整后的 bytes 转成同形。
            want = _normalize_value(col, want)
            if want is not None:
                want = {"hex": want.hex()}
        if got != want:
            return False
    if starts_with is not None:
        got = values[schema[k]["name"]]
        if not isinstance(got, str) or not got.startswith(starts_with):
            return False
    return True
