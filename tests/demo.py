"""用 fixtures/ 下的给定样例做端到端核对（全顺序 + 四个前缀查询）。

运行：python -m tests.demo
"""

from __future__ import annotations

import json
import os
import tempfile

from orderkey import Store, decode, encode, reference_sort
from orderkey.codec import compile_prefix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIX = os.path.join(ROOT, "fixtures")

# 独立参考排序给出的期望全序（逐列声明比较，row_id 决胜）。
EXPECTED_ORDER = ["r1", "r3", "r8", "r4", "r2", "r5", "r6", "r7"]

# 四个前缀查询的期望命中（id -> row_id 顺序）。
EXPECTED_QUERIES = {
    "tenant_A": ["r3", "r8", "r4", "r2"],
    "tenant_A_score5": ["r3", "r8", "r4"],
    "text_starts_A": ["r3", "r8", "r4", "r2", "r5", "r6"],
    "null_tenant": ["r1"],
}


def load_fixtures() -> tuple[list[dict], list[dict], list[dict]]:
    with open(os.path.join(FIX, "schema.json"), encoding="utf-8") as f:
        schema = json.load(f)
    with open(os.path.join(FIX, "rows.jsonl"), encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    with open(os.path.join(FIX, "queries.json"), encoding="utf-8") as f:
        queries = json.load(f)
    return schema, rows, queries


def check(cond: bool, msg: str) -> None:
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {msg}")
    if not cond:
        raise AssertionError(msg)


def main() -> None:
    schema, rows, queries = load_fixtures()

    print("== 1. 编码可逆性与逐值区分 ==")
    blobs = {}
    for row in rows:
        values = {col["name"]: row[col["name"]] for col in schema}
        blob = encode(schema, values)
        restored = decode(schema, blob)
        check(restored == values, f"{row['row_id']}: decode(encode(x)) == x  blob={blob.hex()}")
        blobs[row["row_id"]] = blob
    check(len(set(blobs.values())) == 7, "8 行中 r3/r8 业务键相同 -> 7 个不同 BLOB")
    check(blobs["r3"] == blobs["r8"], "r3 与 r8 业务键编码完全一致")

    print("\n== 2. 全顺序：encode 字节序 vs 独立参考排序 ==")
    blob_order = [r["row_id"] for r in sorted(rows, key=lambda r: (blobs[r["row_id"]], r["row_id"].encode()))]
    ref_order = [r["row_id"] for r in reference_sort(schema, rows)]
    check(blob_order == ref_order, f"BLOB 排序与参考排序一致: {blob_order}")
    check(ref_order == EXPECTED_ORDER, f"与给定样例期望全序一致: {ref_order}")

    print("\n== 3. build + 四个前缀区间扫描 ==")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "index.db")
        store = Store.build(db_path, schema, rows)
        check(store.count() == 8, f"build 写入 8 行（实际 {store.count()}）")

        for q in queries:
            eq = q.get("equals_prefix", [])
            sw = q.get("starts_with")
            lower, upper = compile_prefix(schema, eq, sw)
            result = store.scan(eq, sw)
            got = [r["row_id"] for r in result.rows]
            print(f"\n  查询 {q['id']}: equals={eq!r} starts_with={sw!r}")
            print(f"    字节界: {result.describe_bounds()}")
            print(f"    SQL  : {result.sql}")
            for line in result.explain:
                print(f"    PLAN : {line}")
            print(f"    读取键数={result.keys_read}  返回键数={result.keys_returned}  命中={got}")
            check(got == EXPECTED_QUERIES[q["id"]], f"{q['id']} 命中行与期望一致")
            check(result.keys_read == result.keys_returned, f"{q['id']} 区间紧密，残余核对无丢弃")

        print("\n== 4. 重开：仅从库内编码/schema 还原并继续查询 ==")
        store.close()
        reopened = Store.open(db_path)  # 不传 schema，完全依赖库内 meta
        check(reopened.schema == schema, "重开后从 meta 还原的 schema 与原 schema 相等")
        result = reopened.scan(["A", 5])
        check(
            [r["row_id"] for r in result.rows] == EXPECTED_QUERIES["tenant_A_score5"],
            "重开库上 tenant=A,score=5 查询结果不变",
        )
        check(
            decode(reopened.schema, reopened._conn.execute(
                "SELECT key_blob FROM kv WHERE row_id='r5'").fetchone()[0]
            )["tenant"] == "A\x00B",
            "仅凭 key_blob 解码还原内嵌零字节文本",
        )

        print("\n== 5. 窄前缀读取量少于全表 ==")
        full = reopened.scan()
        narrow = reopened.scan(["A", 5])
        print(f"    全表读取={full.keys_read}  窄前缀读取={narrow.keys_read}")
        check(narrow.keys_read < full.keys_read, "窄前缀区间实际读取键数少于全表扫描")
        reopened.close()

    print("\n全部样例核对通过。")


if __name__ == "__main__":
    main()
