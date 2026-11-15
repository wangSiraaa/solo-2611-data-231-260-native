"""随机小数据枚举测试：无遗漏、无混入；窄前缀读取量严格小于全表。

策略：枚举一个小规模笛卡尔积（含 NULL、空串、内嵌零字节、负整数），
插入少量重复业务键；枚举所有起始列等值前缀（含 starts_with），用 Python
全量过滤结果作为基准，与 SQLite BLOB 区间扫描结果逐行对比，并核对读取计数。
"""

from __future__ import annotations

import itertools
import random

from orderkey import Store, decode, encode, reference_sort
from orderkey.codec import matches

SCHEMA = [
    {"name": "grp", "type": "text", "order": "asc", "nulls": "first"},
    {"name": "n", "type": "int64", "order": "desc", "nulls": "last"},
    {"name": "t", "type": "text", "order": "desc", "nulls": "first"},
    {"name": "b", "type": "bytes", "order": "asc", "nulls": "first"},
]

GRP_VALUES = [None, "", "A", "AA", "中"]
N_VALUES = [None, -9223372036854775808, -1, 0, 42, 9223372036854775807]
T_VALUES = [None, "", "A", "AA", "AB", "A\x00Z", "中"]
B_VALUES = [None, {"hex": ""}, {"hex": "00"}, {"hex": "00ff"}, {"hex": "ff"}]

# starts_with 候选（覆盖短前缀、空串前缀、含零字节与多字节 UTF-8）。
SW_CANDIDATES = [None, "", "A", "AA", "中", "A\x00"]


def _build_rows() -> list[dict]:
    rows = []
    idx = 0
    for grp, n, t, b in itertools.product(GRP_VALUES, N_VALUES, T_VALUES, B_VALUES):
        rows.append({"row_id": f"id{idx:04d}", "grp": grp, "n": n, "t": t, "b": b})
        idx += 1
    # 固定随机种子：抽 5 个业务键各复制一份（不同 row_id），验证重复键稳定输出。
    rng = random.Random(20261115)
    for row in rng.sample(rows, 5):
        dup = dict(row)
        dup["row_id"] = row["row_id"] + "-dup"
        rows.append(dup)
    return rows


def _python_filter(rows, equals_prefix, starts_with):
    out = []
    for row in rows:
        values = {c["name"]: row[c["name"]] for c in SCHEMA}
        if matches(SCHEMA, values, equals_prefix, starts_with):
            out.append(row)
    return out


def test_enumerated_prefixes(tmp_path):
    rows = _build_rows()
    store = Store.build(str(tmp_path / "rand.db"), SCHEMA, rows)
    total = store.count()
    assert total == len(rows)

    # --- 全顺序：BLOB 字节序 == 独立参考排序，且每条键 decode 还原 ---
    blobs = {}
    for row in rows:
        values = {c["name"]: row[c["name"]] for c in SCHEMA}
        blob = encode(SCHEMA, values)
        assert decode(SCHEMA, blob) == values
        blobs[row["row_id"]] = blob
    blob_order = [r["row_id"] for r in sorted(rows, key=lambda r: (blobs[r["row_id"]], r["row_id"].encode()))]
    ref_order = [r["row_id"] for r in reference_sort(SCHEMA, rows)]
    assert blob_order == ref_order

    # --- 枚举所有等值前缀（前缀元素取自各列真实取值）---
    eq_domains = [
        GRP_VALUES,          # 前 1 列
        N_VALUES,            # 前 2 列
        T_VALUES,            # 前 3 列
        B_VALUES,            # 前 4 列（完整 tuple）
    ]
    cases = []
    for depth, domain in enumerate(eq_domains, start=1):
        for combo in itertools.product(*[d for d in eq_domains[:depth]]):
            cases.append((list(combo), None))

    # --- starts_with 只能挂在紧接等值列之后的 text 列（grp 或 t）---
    # 非法位置：第 2 列 n 是 int64，必须被编译期拒绝。
    illegal_sw_cases = [([grp], "x") for grp in GRP_VALUES if grp is not None]
    # 合法位置：列 0(grp, text) 或列 2(t, text)。
    valid_sw_cases = []
    for sw in SW_CANDIDATES:
        if sw is not None:
            valid_sw_cases.append(([], sw))                       # 作用于 grp
    for grp in GRP_VALUES:
        for n in N_VALUES:
            for sw in SW_CANDIDATES:
                if sw is not None:
                    valid_sw_cases.append(([grp, n], sw))         # 作用于 t

    narrow_verified = 0
    for equals, sw in cases:
        result = store.scan(equals, sw)
        expected_rows = _python_filter(rows, equals, sw)
        expected_ids = [r["row_id"] for r in reference_sort(SCHEMA, expected_rows)]
        got_ids = [r["row_id"] for r in result.rows]

        # 无遗漏、无混入（残余核对后结果必须与 Python 全量过滤完全一致）。
        assert got_ids == expected_ids, f"equals={equals} sw={sw}"
        # 区间读取量不小于返回量（残余核对只会丢弃越界候选）。
        assert result.keys_read >= result.keys_returned
        # 窄前缀（至少一列约束且不是全表）实际读取必须少于全表。
        if equals or sw is not None:
            assert result.keys_read < total, f"窄前缀未收窄扫描: {equals} {sw}"
            narrow_verified += 1

    for equals, sw in valid_sw_cases:
        result = store.scan(equals, sw)
        expected_rows = _python_filter(rows, equals, sw)
        expected_ids = [r["row_id"] for r in reference_sort(SCHEMA, expected_rows)]
        assert [r["row_id"] for r in result.rows] == expected_ids
        assert result.keys_read < total
        narrow_verified += 1

    # starts_with 挂在非 text 紧接列上必须被拒绝。
    import pytest
    for equals, sw in illegal_sw_cases:
        with pytest.raises(ValueError):
            store.scan(equals, sw)
    with pytest.raises(ValueError):
        store.scan([GRP_VALUES[1], 0, T_VALUES[1], B_VALUES[1]], "x")  # 后面已无列

    # 全表扫描读取全部键，且无穷上界在 SQL 中不出现上界子句。
    full = store.scan()
    assert full.keys_read == total and full.upper_hex is None
    assert "key_blob <" not in full.sql
    assert narrow_verified > 100  # 确保确实枚举了足量窄前缀场景

    # --- 重复业务键：同一 BLOB 下按 row_id 字节序稳定输出 ---
    # 直接验证一对已知重复键的相邻与 row_id 次序。
    any_dup_key = None
    for row in rows:
        if row["row_id"].endswith("-dup"):
            any_dup_key = row
            break
    values = {c["name"]: any_dup_key[c["name"]] for c in SCHEMA}
    key_blob = encode(SCHEMA, values)
    siblings = store._conn.execute(
        "SELECT row_id FROM kv WHERE key_blob = ? ORDER BY row_id", (key_blob,)
    ).fetchall()
    assert len(siblings) == 2
    assert siblings[0][0] < siblings[1][0]

    store.close()


def test_reopen_and_persist(tmp_path):
    """重开后仅凭库内编码与 meta 即可 decode 与查询，schema 摘要不一致被拒。"""
    rows = _build_rows()[:50]
    db = str(tmp_path / "p.db")
    store = Store.build(db, SCHEMA, rows)
    before = [r["row_id"] for r in store.scan(["A"]).rows]
    store.close()

    reopened = Store.open(db)  # 不传 schema
    after = [r["row_id"] for r in reopened.scan(["A"]).rows]
    assert before == after
    blob = reopened._conn.execute("SELECT key_blob FROM kv LIMIT 1").fetchone()[0]
    decoded = decode(reopened.schema, blob)
    assert set(decoded.keys()) == {c["name"] for c in SCHEMA}

    wrong_schema = [dict(c) for c in SCHEMA]
    wrong_schema[0]["nulls"] = "last"
    import pytest
    with pytest.raises(ValueError):
        Store.open(db, wrong_schema)
    reopened.close()
