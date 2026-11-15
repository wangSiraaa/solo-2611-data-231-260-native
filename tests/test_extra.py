"""补充测试：desc 文本短串/长串、边界值区分、非法输入拒绝、successor 边界。"""

from __future__ import annotations

import pytest

from orderkey import INT64_MAX, INT64_MIN, decode, encode, reference_sort
from orderkey.codec import _successor, compile_prefix, validate_schema
from orderkey.store import Store


def test_desc_text_short_long_order(tmp_path):
    """desc 文本列：短串是长串前缀时，终止符参与次序，短串必须排在长串之后。"""
    schema = [
        {"name": "t", "type": "text", "order": "desc", "nulls": "first"},
    ]
    texts = ["", "A", "AA", "AAA", "AB", "中", "A\x00B"]
    rows = [{"row_id": f"r{i}", "t": t} for i, t in enumerate(texts)]
    # NULL 行：nulls first + desc，NULL 应在最前。
    rows.append({"row_id": "rnull", "t": None})

    blobs = {r["row_id"]: encode(schema, r) for r in rows}
    for r in rows:
        assert decode(schema, blobs[r["row_id"]]) == {"t": r["t"]}

    blob_order = [
        r["row_id"] for r in sorted(rows, key=lambda r: (blobs[r["row_id"]], r["row_id"].encode()))
    ]
    ref_order = [r["row_id"] for r in reference_sort(schema, rows)]
    assert blob_order == ref_order
    # 期望：NULL 最前，其后按 UTF-8 字节序逆序；短前缀 A 位于 AA/AAA 之后。
    expected_values = [None, "中", "AB", "AAA", "AA", "A\x00B", "A", ""]
    got_values = [next(r["t"] for r in rows if r["row_id"] == rid) for rid in ref_order]
    assert got_values == expected_values

    # 区间扫描核对：starts_with="A" 在 desc 列上仍精确命中 A/AA/AAA/AB/A\\x00B。
    store = Store.build(str(tmp_path / "d.db"), schema, rows)
    result = store.scan([], starts_with="A")
    got = sorted(r["key"]["t"] for r in result.rows)
    assert got == sorted(["A", "AA", "AAA", "AB", "A\x00B"])
    # 前缀不能带完整字段终止符。desc+nulls first：值标签 0x02 -> 取反 0xFD
    # -> 头部交换(0xFD<->0xFF) => 0xFF；负载 'A' 仅取反 => 0xBE；不含终止符。
    lower, upper = compile_prefix(schema, [], starts_with="A")
    assert lower == bytes([0xFF, 0xFF ^ ord("A")])
    # desc+nulls last：不做头部交换，值标签最终为 0xFD。
    schema_last = [{"name": "t", "type": "text", "order": "desc", "nulls": "last"}]
    lower_last, _ = compile_prefix(schema_last, [], starts_with="A")
    assert lower_last == bytes([0xFD, 0xFF ^ ord("A")])
    store.close()


def test_edge_values_are_all_distinct():
    """负整数/最小最大 int64、空串、空 bytes、内嵌零字节、NULL 编码互不相同。"""
    schema = [
        {"name": "i", "type": "int64", "order": "asc", "nulls": "first"},
        {"name": "t", "type": "text", "order": "asc", "nulls": "first"},
        {"name": "b", "type": "bytes", "order": "asc", "nulls": "first"},
    ]
    keys = [
        {"i": INT64_MIN, "t": "", "b": {"hex": ""}},
        {"i": -1, "t": "", "b": {"hex": ""}},
        {"i": 0, "t": "", "b": {"hex": ""}},
        {"i": 1, "t": "", "b": {"hex": ""}},
        {"i": INT64_MAX, "t": "", "b": {"hex": ""}},
        {"i": None, "t": "", "b": {"hex": ""}},
        {"i": 0, "t": None, "b": {"hex": ""}},
        {"i": 0, "t": "", "b": None},
        {"i": 0, "t": "a", "b": {"hex": ""}},
        {"i": 0, "t": "a", "b": {"hex": "00"}},
        {"i": 0, "t": "a", "b": {"hex": "0000"}},
        {"i": 0, "t": "a\x00", "b": {"hex": "01"}},
        {"i": 0, "t": "a", "b": {"hex": "01"}},
    ]
    blobs = [encode(schema, k) for k in keys]
    assert len(set(blobs)) == len(blobs)
    for k, blob in zip(keys, blobs):
        assert decode(schema, blob) == k

    # int64 单调性（含负数）。
    ints = [INT64_MIN, -2, -1, 0, 1, 2, INT64_MAX]
    int_blobs = [
        encode([schema[0]], {"i": v}) for v in ints
    ]
    assert int_blobs == sorted(int_blobs)


def test_desc_int64_and_null_positions():
    schema = [{"name": "i", "type": "int64", "order": "desc", "nulls": "last"}]
    vals = [None, -5, 0, 5, INT64_MIN, INT64_MAX]
    rows = [{"row_id": str(i), "i": v} for i, v in enumerate(vals)]
    blobs = [encode(schema, {"i": r["i"]}) for r in rows]
    order = [r["i"] for _, r in sorted(zip(blobs, rows))]
    assert order == [INT64_MAX, 5, 0, -5, INT64_MIN, None]

    schema_nf = [{"name": "i", "type": "int64", "order": "desc", "nulls": "first"}]
    rows_nf = [{"row_id": str(i), "i": v} for i, v in enumerate(vals)]
    blobs_nf = [encode(schema_nf, {"i": r["i"]}) for r in rows_nf]
    order_nf = [r["i"] for _, r in sorted(zip(blobs_nf, rows_nf))]
    assert order_nf == [None, INT64_MAX, 5, 0, -5, INT64_MIN]


def test_reject_bad_inputs():
    schema = [
        {"name": "i", "type": "int64", "order": "asc", "nulls": "first"},
        {"name": "t", "type": "text", "order": "asc", "nulls": "first"},
        {"name": "b", "type": "bytes", "order": "asc", "nulls": "first"},
    ]
    with pytest.raises(ValueError):
        encode(schema, {"i": True, "t": "x", "b": {"hex": ""}})          # bool 不算 int
    with pytest.raises(ValueError):
        encode(schema, {"i": INT64_MIN - 1, "t": "x", "b": {"hex": ""}})  # 超界
    with pytest.raises(ValueError):
        encode(schema, {"i": 1, "t": "x", "b": {"hex": "AB"}})            # 大写
    with pytest.raises(ValueError):
        encode(schema, {"i": 1, "t": "x", "b": {"hex": "abc"}})           # 奇数位
    with pytest.raises(ValueError):
        encode(schema, {"i": 1, "t": "x", "b": {"hex": "zz"}})            # 非法字符
    with pytest.raises(ValueError):
        encode(schema, {"i": 1, "t": "x", "b": "ab"})                      # 错误结构
    with pytest.raises(ValueError):
        encode(schema, {"i": 1, "t": 123, "b": {"hex": ""}})              # 错误值类型
    with pytest.raises(ValueError):
        encode(schema, {"i": 1, "t": "x"})                                # 缺列
    with pytest.raises(ValueError):
        validate_schema([{"name": "x", "type": "float", "order": "asc", "nulls": "first"}])
    with pytest.raises(ValueError):
        validate_schema([{"name": "x", "type": "int64", "order": "up", "nulls": "first"}])


def test_successor_and_infinite_bound():
    assert _successor(b"") is None  # 空前缀覆盖整个键空间，无有限后继 -> 无穷
    assert _successor(b"\x01\x02") == b"\x01\x03"
    assert _successor(b"\x01\xff") == b"\x02"
    assert _successor(b"\xff") is None
    assert _successor(b"\xff\xff") is None

    # 全表扫描：空前缀 -> 下界空 BLOB，上界为无穷（None），SQL 不写上界。
    schema = [{"name": "t", "type": "text", "order": "asc", "nulls": "first"}]
    lower, upper = compile_prefix(schema, [])
    assert lower == b"" and upper is None


def test_utf8_binary_order_no_normalization():
    # 不同 Unicode 码点序列按 UTF-8 原始字节排序，不做任何归一化。
    precomposed = "\u00e9"              # U+00E9 单码点
    combining = "e\u0301"             # U+0065 + U+0301 两码点
    schema = [{"name": "t", "type": "text", "order": "asc", "nulls": "first"}]
    vals = [precomposed, combining, "中", "A"]
    rows = [{"row_id": str(i), "t": v} for i, v in enumerate(vals)]
    ref = [r["t"] for r in reference_sort(schema, rows)]
    blob_order = [
        r["t"]
        for r in sorted(rows, key=lambda r: (encode(schema, r), r["row_id"].encode()))
    ]
    assert blob_order == ref
    assert encode(schema, {"t": precomposed}) != encode(schema, {"t": combining})
    # 二进制序与直接比较 UTF-8 字节一致。
    assert precomposed.encode("utf-8") != combining.encode("utf-8")
