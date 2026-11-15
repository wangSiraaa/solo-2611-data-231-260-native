#!/usr/bin/env python3
"""只读 SQLite 表抽取器。

直接按 SQLite 文件格式规范（file format 2.0）解析 .sqlite 文件的真实页，
抽取 table.json 指定的普通 rowid 表，导出 records.jsonl（每行一条记录，
含逻辑列值与物理来源）。核心抽取不调用 sqlite3 模块、现成解析器或外部
sqlite 命令；仅使用 Python 3.11 标准库。

支持范围（超出即明确报错，不静默补空、不漏行）：
  * SQLite3 文件、UTF-8 文本编码、rollback 模式（非 WAL）；
  * 表 b-tree：叶页 0x0d、内部页 0x05；普通 rowid 表；
  * serial type：NULL、1/2/3/4/6/8 字节有符号整数、float64、常量 0/1、
    BLOB、UTF-8 TEXT；INTEGER PRIMARY KEY 列逻辑值取自 rowid。
不支持：索引 b-tree（0x02/0x0a）、WITHOUT ROWID 表、WAL、已删数据恢复。
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"SQLite format 3\x00"
PAGE_TYPE_INTERIOR_TABLE = 0x05  # 表 b-tree 内部页
PAGE_TYPE_LEAF_TABLE = 0x0D      # 表 b-tree 叶页
MIN_USABLE_SIZE = 480            # SQLite 规定每页可用空间至少 480 字节
DB_HEADER_SIZE = 100             # 第 1 页前 100 字节为数据库头


class CorruptError(Exception):
    """输入损坏或超出支持范围时抛出；消息带页号/偏移等定位信息。"""


# --------------------------------------------------------------------------
# varint（SQLite 变长整数：大端 base-128，最多 9 字节）
# --------------------------------------------------------------------------

def parse_varint(buf: bytes | bytearray, pos: int, end: int, ctx: str) -> tuple[int, int]:
    """解析 [pos, end) 内的一个 varint，返回 (无符号 64 位值, 下一字节位置)。

    前 8 个字节各贡献低 7 位，最高位为续位（1 表示还有后续字节）；
    若前 8 字节续位均置位，则第 9 字节贡献完整 8 位，合计 64 位。
    """
    value = 0
    for i in range(9):
        if pos + i >= end:
            raise CorruptError(f"{ctx}: varint 在偏移 {pos + i} 处被截断")
        b = buf[pos + i]
        if i == 8:  # 第 9 字节：取全部 8 位，无论续位
            value = (value << 8) | b
            return value, pos + 9
        value = (value << 7) | (b & 0x7F)
        if not b & 0x80:
            return value, pos + i + 1
    raise AssertionError("unreachable")


def to_int64(value: int) -> int:
    """把 64 位无符号值按二进制补码解释为有符号 64 位整数（rowid 用）。"""
    return value - (1 << 64) if value >= (1 << 63) else value


# --------------------------------------------------------------------------
# 数据库头（第 1 页前 100 字节，多字节字段均为大端）
# --------------------------------------------------------------------------

@dataclass
class DbHeader:
    page_size: int        # 页大小（字节）
    reserved: int         # 每页末尾保留字节数
    usable_size: int      # page_size - reserved
    page_count: int       # 实际页数（文件大小 / 页大小）
    encoding: int         # 1=UTF-8, 2=UTF-16le, 3=UTF-16be


def parse_db_header(data: bytes) -> DbHeader:
    if len(data) < DB_HEADER_SIZE:
        raise CorruptError(f"文件仅 {len(data)} 字节，不足 100 字节数据库头")
    if data[:16] != MAGIC:
        raise CorruptError(f"文件头魔数不符：{data[:16]!r}，期望 {MAGIC!r}")

    raw_ps = struct.unpack_from(">H", data, 16)[0]      # 偏移 16：页大小，1 表示 65536
    page_size = 65536 if raw_ps == 1 else raw_ps
    if page_size < 512 or page_size > 65536 or page_size & (page_size - 1):
        raise CorruptError(f"非法页大小 {page_size}（须为 512..65536 之间 2 的幂）")

    write_ver, read_ver = data[18], data[19]            # 偏移 18/19：文件格式版本
    if write_ver not in (1, 2) or read_ver not in (1, 2):
        raise CorruptError(f"非法文件格式版本 write={write_ver} read={read_ver}")
    if write_ver == 2 or read_ver == 2:
        raise CorruptError("数据库为 WAL 模式（文件格式版本 2），本工具不支持")

    reserved = data[20]                                 # 偏移 20：每页保留字节
    usable_size = page_size - reserved
    if usable_size < MIN_USABLE_SIZE:
        raise CorruptError(
            f"每页可用空间 {usable_size} 字节（页大小 {page_size} - 保留 {reserved}）"
            f"小于下限 {MIN_USABLE_SIZE}"
        )

    if len(data) % page_size != 0:
        raise CorruptError(
            f"文件大小 {len(data)} 不是页大小 {page_size} 的整数倍，"
            f"最后一页不完整（疑似截断）"
        )
    page_count = len(data) // page_size
    if page_count < 1:
        raise CorruptError("文件不足一页")

    # 偏移 24：change counter；偏移 28：头内数据库页数；偏移 92：version-valid-for。
    # 头内页数仅在 change counter == version-valid-for 且非零时有效，有效则必须与实际一致。
    change_counter = struct.unpack_from(">I", data, 24)[0]
    in_header_pages = struct.unpack_from(">I", data, 28)[0]
    version_valid_for = struct.unpack_from(">I", data, 92)[0]
    if in_header_pages and change_counter == version_valid_for and in_header_pages != page_count:
        raise CorruptError(
            f"头内数据库大小 {in_header_pages} 页与实际 {page_count} 页不符（文件可能被截断）"
        )

    encoding = struct.unpack_from(">I", data, 56)[0]    # 偏移 56：文本编码
    if encoding not in (1, 2, 3):
        raise CorruptError(f"非法文本编码标记 {encoding}")
    if encoding != 1:
        raise CorruptError(f"仅支持 UTF-8 数据库（encoding=1），实际 encoding={encoding}")

    return DbHeader(page_size, reserved, usable_size, page_count, encoding)


# --------------------------------------------------------------------------
# 表描述（table.json）
# --------------------------------------------------------------------------

@dataclass
class TableSpec:
    name: str
    rootpage: int
    columns: list[str]
    ipk_index: int | None   # INTEGER PRIMARY KEY 列下标；None 表示无
    page_size: int | None   # 期望页大小（可选，用于与数据库头交叉核对）


def load_table_spec(path: str | Path) -> TableSpec:
    try:
        obj = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise CorruptError(f"表描述 {path} 不是合法 JSON：{e}") from e
    if not isinstance(obj, dict):
        raise CorruptError(f"表描述 {path} 顶层必须是 JSON 对象")

    def _require(key: str):
        if key not in obj:
            raise CorruptError(f"表描述 {path} 缺少字段 {key!r}")
        return obj[key]

    name = _require("table")
    rootpage = _require("rootpage")
    columns = _require("columns")
    ipk_index = obj.get("integer_primary_key_index")
    if not isinstance(name, str) or not name:
        raise CorruptError(f"表描述字段 'table' 必须是非空字符串，实际 {name!r}")
    if not isinstance(rootpage, int) or isinstance(rootpage, bool) or rootpage < 1:
        raise CorruptError(f"表描述字段 'rootpage' 必须是 >= 1 的整数，实际 {rootpage!r}")
    if (not isinstance(columns, list) or not columns
            or not all(isinstance(c, str) for c in columns)):
        raise CorruptError(f"表描述字段 'columns' 必须是非空字符串数组，实际 {columns!r}")
    if len(set(columns)) != len(columns):
        raise CorruptError(f"表描述字段 'columns' 存在重复列名：{columns!r}")
    if ipk_index is not None:
        if not isinstance(ipk_index, int) or isinstance(ipk_index, bool) \
                or not 0 <= ipk_index < len(columns):
            raise CorruptError(
                f"integer_primary_key_index 必须是 [0, {len(columns)}) 内整数或 null，"
                f"实际 {ipk_index!r}"
            )
    encoding = obj.get("encoding", "UTF-8")
    if encoding != "UTF-8":
        raise CorruptError(f"仅支持 encoding='UTF-8'，表描述声明 {encoding!r}")
    blob_json = obj.get("blob_json", "hex lowercase")
    if blob_json != "hex lowercase":
        raise CorruptError(f"仅支持 blob_json='hex lowercase'，表描述声明 {blob_json!r}")
    page_size = obj.get("page_size")
    if page_size is not None and (not isinstance(page_size, int) or page_size <= 0):
        raise CorruptError(f"表描述字段 'page_size' 非法：{page_size!r}")
    return TableSpec(name, rootpage, list(columns), ipk_index, page_size)


# --------------------------------------------------------------------------
# 表叶 cell 的页内载荷大小（SQLite file format 2.0 规定公式，仅表 b-tree）
# --------------------------------------------------------------------------

def local_payload_size(payload_len: int, usable_size: int) -> int:
    """返回表叶 cell 存放在页内的载荷字节数，其余经 overflow 页链存放。

    maxLocal = usableSize - 35
    minLocal = ((usableSize - 12) * 32 // 255) - 23
    P <= maxLocal：全部在页内；
    否则令 K = minLocal + (P - minLocal) % (usableSize - 4)，
    K <= maxLocal 时页内存 K 字节，否则页内存 minLocal 字节。
    """
    max_local = usable_size - 35
    min_local = ((usable_size - 12) * 32 // 255) - 23
    if payload_len <= max_local:
        return payload_len
    k = min_local + (payload_len - min_local) % (usable_size - 4)
    return k if k <= max_local else min_local


# --------------------------------------------------------------------------
# record（行载荷）解析：header 长度 varint + serial type 序列 + 数据体
# --------------------------------------------------------------------------

# serial type 0..9 的定长字节数（10、11 为保留，单独报错）
_SERIAL_FIXED_SIZE = {0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 6, 6: 8, 7: 8, 8: 0, 9: 0}


def _decode_value(serial_type: int, raw: bytes, ctx: str):
    """把一个字段的字节串按 serial type 解码为 Python 值（BLOB 返回 bytes）。"""
    if serial_type == 0:
        return None
    if 1 <= serial_type <= 6:            # 1/2/3/4/6/8 字节大端有符号整数
        return int.from_bytes(raw, "big", signed=True)
    if serial_type == 7:                 # 8 字节大端 IEEE 754 双精度
        return struct.unpack(">d", raw)[0]
    if serial_type == 8:
        return 0
    if serial_type == 9:
        return 1
    if serial_type >= 13 and serial_type % 2 == 1:   # 奇数 >=13：UTF-8 TEXT
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise CorruptError(f"{ctx}: TEXT 字段不是合法 UTF-8：{e}") from e
    # 偶数 >=12：BLOB
    return bytes(raw)


def parse_record(payload: bytes, columns: list[str], ipk_index: int | None,
                 rowid: int, ctx: str) -> dict[str, object]:
    """解析完整行载荷，返回 {列名: 值}。

    布局：header 长度 varint（含自身）→ 各列 serial type varint → 数据体。
    header 字节数 + 各字段字节数之和必须恰好等于 payload 长度。
    INTEGER PRIMARY KEY 列在 record 中存 NULL，逻辑值取自 rowid。
    """
    if not payload:
        raise CorruptError(f"{ctx}: 载荷为空，无法容纳 record header")
    header_len, pos = parse_varint(payload, 0, len(payload), ctx)
    if header_len < 1 or header_len > len(payload):
        raise CorruptError(
            f"{ctx}: record header 长度 {header_len} 非法（载荷共 {len(payload)} 字节）"
        )

    serial_types: list[int] = []
    while pos < header_len:
        st, pos = parse_varint(payload, pos, header_len, ctx)
        serial_types.append(st)
    if len(serial_types) > len(columns):
        raise CorruptError(
            f"{ctx}: record 含 {len(serial_types)} 个字段，超过表定义的 {len(columns)} 列"
        )

    values: list[object] = []
    body = header_len
    for i, st in enumerate(serial_types):
        if st in (10, 11):
            raise CorruptError(f"{ctx}: 列 {i} 使用保留 serial type {st}")
        size = _SERIAL_FIXED_SIZE[st] if st < 12 else (st - 12) // 2
        if body + size > len(payload):
            raise CorruptError(
                f"{ctx}: 列 {i}（serial type {st}，需 {size} 字节）超出载荷末尾，"
                f"仅剩 {len(payload) - body} 字节"
            )
        values.append(_decode_value(st, payload[body:body + size], ctx))
        body += size
    if body != len(payload):
        raise CorruptError(
            f"{ctx}: record header 与各字段共消费 {body} 字节，"
            f"与载荷长度 {len(payload)} 不一致"
        )

    # ALTER TABLE ADD COLUMN 后旧行缺少尾列，按 NULL 处理
    while len(values) < len(columns):
        values.append(None)
    if ipk_index is not None:
        values[ipk_index] = rowid
    return dict(zip(columns, values))


# --------------------------------------------------------------------------
# b-tree 遍历与 cell 读取
# --------------------------------------------------------------------------

@dataclass
class RawRecord:
    rowid: int
    payload: bytes
    leaf_page: int
    cell_index: int          # 叶页 cell 指针数组中的下标（0 起）
    overflow_pages: list[int]


class TableExtractor:
    """从完整读入内存的数据库字节流中抽取一张 rowid 表。"""

    def __init__(self, data: bytes, header: DbHeader, spec: TableSpec):
        if spec.page_size is not None and spec.page_size != header.page_size:
            raise CorruptError(
                f"表描述声明页大小 {spec.page_size}，数据库头实际为 {header.page_size}"
            )
        if not 1 <= spec.rootpage <= header.page_count:
            raise CorruptError(
                f"rootpage {spec.rootpage} 越界：数据库共 {header.page_count} 页"
            )
        self.data = data
        self.header = header
        self.spec = spec

    # -- 基础读取 ----------------------------------------------------------

    def _page(self, page_no: int) -> bytes:
        """取一页（页号 1 起）。文件大小已校验为页大小整数倍，切片必然完整。"""
        base = (page_no - 1) * self.header.page_size
        return self.data[base:base + self.header.page_size]

    def _check_page_no(self, page_no: int, ctx: str) -> None:
        if not 1 <= page_no <= self.header.page_count:
            raise CorruptError(
                f"{ctx}: 页号 {page_no} 越界（合法范围 1..{self.header.page_count}）"
            )

    @staticmethod
    def _u16(page: bytes, off: int, ctx: str) -> int:
        if off < 0 or off + 2 > len(page):
            raise CorruptError(f"{ctx}: 2 字节读取越界（页内偏移 {off}）")
        return struct.unpack_from(">H", page, off)[0]

    @staticmethod
    def _u32(page: bytes, off: int, ctx: str) -> int:
        if off < 0 or off + 4 > len(page):
            raise CorruptError(f"{ctx}: 4 字节读取越界（页内偏移 {off}）")
        return struct.unpack_from(">I", page, off)[0]

    # -- b-tree 遍历 --------------------------------------------------------

    def walk(self) -> list[RawRecord]:
        """按 rowid 升序返回全部叶 cell。"""
        records: list[RawRecord] = []
        self._walk_btree(self.spec.rootpage, set(), records, depth=0)
        # 遍历本身已按 b-tree 逻辑顺序进行；此处再显式排序，确保输出按 rowid 升序，
        # 而不是依赖页内物理顺序。
        records.sort(key=lambda r: r.rowid)
        return records

    def _walk_btree(self, page_no: int, visited: set[int],
                    records: list[RawRecord], depth: int) -> None:
        ctx = f"第 {page_no} 页"
        self._check_page_no(page_no, f"{ctx}（b-tree 遍历）")
        if page_no in visited:
            raise CorruptError(f"b-tree 遍历出现环：第 {page_no} 页被重复访问")
        if depth > self.header.page_count:
            raise CorruptError("b-tree 深度超过数据库页数，文件损坏")
        visited.add(page_no)

        page = self._page(page_no)
        hdr = DB_HEADER_SIZE if page_no == 1 else 0   # 第 1 页 b-tree 头在 100 字节数据库头之后
        ptype = page[hdr]
        if ptype not in (PAGE_TYPE_INTERIOR_TABLE, PAGE_TYPE_LEAF_TABLE):
            raise CorruptError(
                f"{ctx}: 页类型 0x{ptype:02x} 不受支持"
                f"（仅支持表 b-tree 内部页 0x05 / 叶页 0x0d）"
            )

        interior = ptype == PAGE_TYPE_INTERIOR_TABLE
        header_size = 12 if interior else 8           # 内部页多 4 字节最右孩子指针
        ncell = self._u16(page, hdr + 3, ctx)
        ptr_array_end = hdr + header_size + 2 * ncell
        if ptr_array_end > self.header.page_size:
            raise CorruptError(f"{ctx}: {ncell} 个 cell 的指针数组超出页末尾")
        cell_ptrs = [self._u16(page, hdr + header_size + 2 * i, ctx) for i in range(ncell)]
        for i, cp in enumerate(cell_ptrs):
            if cp < ptr_array_end or cp >= self.header.page_size:
                raise CorruptError(
                    f"{ctx}: cell 指针[{i}]={cp} 越界（须位于 [{ptr_array_end}, "
                    f"{self.header.page_size})）"
                )

        if interior:
            # 内部页 cell：4 字节大端左孩子页号 + rowid 键 varint；
            # 头部第 8..11 字节为最右孩子。按 左孩子…最右孩子 顺序递归即得升序叶序列。
            children: list[int] = []
            for i, cp in enumerate(cell_ptrs):
                child = self._u32(page, cp, f"{ctx} cell {i}")
                self._check_page_no(child, f"{ctx} cell {i} 左孩子")
                parse_varint(page, cp + 4, self.header.page_size,
                             f"{ctx} cell {i} rowid 键")  # 校验键 varint 完整
                children.append(child)
            rightmost = self._u32(page, hdr + 8, ctx)
            self._check_page_no(rightmost, f"{ctx} 最右孩子")
            children.append(rightmost)
            for child in children:
                self._walk_btree(child, visited, records, depth + 1)
        else:
            for i, cp in enumerate(cell_ptrs):
                records.append(self._read_leaf_cell(page_no, page, i, cp))

    # -- 叶 cell 与 overflow 链 ---------------------------------------------

    def _read_leaf_cell(self, page_no: int, page: bytes,
                        cell_index: int, cp: int) -> RawRecord:
        ctx = f"第 {page_no} 页 cell {cell_index}"
        page_size = self.header.page_size
        payload_len, pos = parse_varint(page, cp, page_size, ctx)       # 载荷总长度 P
        rowid_u, pos = parse_varint(page, pos, page_size, ctx)          # rowid
        rowid = to_int64(rowid_u)

        local = local_payload_size(payload_len, self.header.usable_size)
        if pos + local > page_size:
            raise CorruptError(
                f"{ctx}: 页内载荷 {local} 字节自偏移 {pos} 起超出页末尾"
            )
        payload = bytearray(page[pos:pos + local])
        pos += local

        overflow_pages: list[int] = []
        if local < payload_len:
            if pos + 4 > page_size:
                raise CorruptError(f"{ctx}: overflow 首页页号指针越界")
            first = self._u32(page, pos, ctx)
            payload += self._read_overflow(first, payload_len - local, overflow_pages, ctx)

        return RawRecord(rowid, bytes(payload), page_no, cell_index, overflow_pages)

    def _read_overflow(self, first_page: int, needed: int,
                       chain_out: list[int], ctx: str) -> bytes:
        """沿 overflow 页链取 needed 字节；链必须恰好在取齐后终止（下一页号 0）。"""
        chunks = bytearray()
        seen: set[int] = set()
        page_no = first_page
        remaining = needed
        last_next: int | None = None
        while remaining > 0:
            self._check_page_no(page_no, f"{ctx} 的 overflow 链")
            if page_no in seen:
                raise CorruptError(f"{ctx}: overflow 链在第 {page_no} 页成环")
            seen.add(page_no)
            chain_out.append(page_no)
            page = self._page(page_no)
            nxt = self._u32(page, 0, f"{ctx} overflow 页 {page_no}")
            take = min(remaining, self.header.usable_size - 4)
            chunks += page[4:4 + take]
            remaining -= take
            last_next = nxt
            page_no = nxt
        if last_next != 0:
            raise CorruptError(
                f"{ctx}: overflow 链在取齐 {needed} 字节后仍未终止"
                f"（第 {chain_out[-1]} 页指向下一页 {last_next}）"
            )
        return bytes(chunks)


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def _json_value(value: object, ctx: str):
    """把 Python 值转成 JSON 可序列化形式：BLOB → 小写 hex 字符串。"""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    if isinstance(value, float) and not math.isfinite(value):
        raise CorruptError(f"{ctx}: REAL 值 {value!r} 不是有限数，无法写入 JSON")
    return value


def extract_records(db_path: str | Path, spec_path: str | Path) -> list[dict]:
    """抽取指定表，返回按 rowid 升序的输出行列表。"""
    data = Path(db_path).read_bytes()
    header = parse_db_header(data)
    spec = load_table_spec(spec_path)
    raw_records = TableExtractor(data, header, spec).walk()

    rows: list[dict] = []
    for rec in raw_records:
        ctx = f"第 {rec.leaf_page} 页 cell {rec.cell_index}（rowid {rec.rowid}）"
        values = parse_record(rec.payload, spec.columns, spec.ipk_index, rec.rowid, ctx)
        rows.append({
            "table": spec.name,
            "rowid": rec.rowid,
            "values": {k: _json_value(v, ctx) for k, v in values.items()},
            "source": {
                "leaf_page": rec.leaf_page,
                "cell_index": rec.cell_index,
                "rowid": rec.rowid,
                "payload_bytes": len(rec.payload),
                "overflow_pages": rec.overflow_pages,
            },
        })
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读 SQLite 表抽取器：直接解析文件页，不调用 sqlite3。"
    )
    parser.add_argument("db", help="SQLite 数据库文件路径")
    parser.add_argument("table", help="表描述 JSON（如 table.json）")
    parser.add_argument("-o", "--out", default="records.jsonl",
                        help="输出 JSONL 路径（默认 records.jsonl）")
    args = parser.parse_args(argv)

    try:
        rows = extract_records(args.db, args.table)
        lines = [json.dumps(r, ensure_ascii=False, allow_nan=False) for r in rows]
    except (CorruptError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    Path(args.out).write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    leaves = sorted({r["source"]["leaf_page"] for r in rows})
    overflow = sorted({p for r in rows for p in r["source"]["overflow_pages"]})
    print(f"ok: {len(rows)} 行 -> {args.out}；叶页 {leaves}；overflow 页 {overflow}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
