"""属性式随机往返验证（确定性种子，纯标准库）。

随机生成符合 schema 的嵌套记录（含 null/空 list/null 元素/不同内层长度），
验证：编码 -> 栈式解码 == 递归参考解码 == 规范化原始记录。
仅用于这个小型 schema 子集，不做大数据压测。
"""

from __future__ import annotations

import json
import random
import unittest
from pathlib import Path

from dremel.decode import StackDecoder, decode_columns
from dremel.encode import ColumnEncoder
from dremel.planner import compile_plans
from dremel.reference import reference_decode
from dremel.schema import load_schema, validate_and_normalize

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = load_schema(json.loads((ROOT / "schema.json").read_text(encoding="utf-8")))
WORDS = ["", "a", "bb", "ccc", "x-y", "中文"]


def _maybe(rng: random.Random, value):
    return None if rng.random() < 0.3 else value


def _gen_row(rng: random.Random, rid: int) -> dict:
    row: dict = {"id": rid}
    # meta：缺失/ null / 空 struct / 带 name
    m = rng.random()
    if m < 0.25:
        row["meta"] = None
    elif m < 0.45:
        row["meta"] = {}
    else:
        row["meta"] = {"name": _maybe(rng, rng.choice(WORDS))}

    # batches：null / 空 / 若干元素
    b = rng.random()
    if b < 0.2:
        row["batches"] = None
    elif b < 0.35:
        row["batches"] = []
    else:
        batches = []
        for _ in range(rng.randint(1, 4)):
            if rng.random() < 0.2:
                batches.append(None)  # null 元素
                continue
            elem: dict = {}
            # code 是 required string
            elem["code"] = rng.choice(WORDS) or "z"
            v = rng.random()
            if v < 0.25:
                elem["values"] = None
            elif v < 0.4:
                elem["values"] = []
            else:
                n = rng.randint(0, 5)  # 不同内层长度，含 0
                elem["values"] = [
                    None if rng.random() < 0.3 else rng.randint(-100, 100)
                    for _ in range(n)
                ]
            batches.append(elem)
        row["batches"] = batches
    return row


class FuzzRoundTripTest(unittest.TestCase):
    def test_many_random_records_three_way_consistency(self):
        rng = random.Random(20261115)
        rows = [_gen_row(rng, i) for i in range(300)]
        enc = ColumnEncoder(SCHEMA)
        normalized = enc.encode_all(rows)
        plans = compile_plans(SCHEMA)

        # 两个独立解码器必须逐叶一致
        for path, entries in enc.columns.items():
            self.assertEqual(
                StackDecoder(plans[path], path, entries).run(),
                reference_decode(plans[path], entries),
            )

        restored = decode_columns(SCHEMA, enc.columns)
        self.assertEqual(len(restored), len(normalized))
        for a, b in zip(normalized, restored):
            self.assertEqual(a, b)

    def test_empty_record_set_roundtrip(self):
        enc = ColumnEncoder(SCHEMA)
        enc.encode_all([])
        self.assertEqual(decode_columns(SCHEMA, enc.columns), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
