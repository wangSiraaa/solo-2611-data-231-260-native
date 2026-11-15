"""字节级健壮性：负载 0x02、截断/多余字节拒绝、极值次序（全 order/nulls 组合）。"""

from __future__ import annotations

import pytest

from orderkey import INT64_MAX, INT64_MIN, decode, encode, reference_sort
from orderkey.codec import compile_prefix


def test_payload_0x02_roundtrip_and_prefix_all_variants():
    keys = [
        {"b": {"hex": "02"}, "t": "\x02"},
        {"b": {"hex": "0200"}, "t": "\x02\x00"},
        {"b": {"hex": "0102"}, "t": "x"},
        {"b": None, "t": None},
        {"b": {"hex": ""}, "t": ""},
    ]
    for order in ("asc", "desc"):
        for nulls in ("first", "last"):
            schema = [
                {"name": "b", "type": "bytes", "order": order, "nulls": nulls},
                {"name": "t", "type": "text", "order": order, "nulls": nulls},
            ]
            blobs = [encode(schema, k) for k in keys]
            assert len(set(blobs)) == len(blobs)
            for k, blob in zip(keys, blobs):
                assert decode(schema, blob) == k

            lo, hi = compile_prefix(schema, [{"hex": "02"}])
            for k, blob in zip(keys, blobs):
                inside = lo <= blob and (hi is None or blob < hi)
                assert inside == (k["b"] == {"hex": "02"})


def test_corrupt_blobs_rejected():
    schema = [
        {"name": "i", "type": "int64", "order": "desc", "nulls": "first"},
        {"name": "t", "type": "text", "order": "asc", "nulls": "last"},
    ]
    blob = encode(schema, {"i": 7, "t": "abc"})
    for bad in (blob[:-1], blob[:3], b"", blob + b"\x00"):
        with pytest.raises(ValueError):
            decode(schema, bad)


def test_int64_extrema_desc_order():
    schema = [{"name": "i", "type": "int64", "order": "desc", "nulls": "last"}]
    rows = [{"row_id": str(v), "i": v}
            for v in (None, INT64_MIN, -1, 0, 1, INT64_MAX)]
    ref = [r["i"] for r in reference_sort(schema, rows)]
    got = [
        r["i"]
        for _, r in sorted((encode(schema, {"i": r["i"]}), r) for r in rows)
    ]
    assert got == ref == [INT64_MAX, 1, 0, -1, INT64_MIN, None]


def test_full_tuple_prefix_is_point_range():
    """完整 tuple 前缀（全部列等值）应退化为单点区间，重复键由 row_id 决胜。"""
    schema = [
        {"name": "g", "type": "text", "order": "asc", "nulls": "first"},
        {"name": "n", "type": "int64", "order": "desc", "nulls": "last"},
    ]
    rows = [
        {"row_id": "a", "g": "A", "n": 1},
        {"row_id": "b", "g": "A", "n": 1},
        {"row_id": "c", "g": "A", "n": 2},
    ]
    import tempfile
    from orderkey.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        store = Store.build(f"{tmp}/p.db", schema, rows)
        result = store.scan(["A", 1])
        assert [r["row_id"] for r in result.rows] == ["a", "b"]
        assert result.keys_read == 2
        store.close()
