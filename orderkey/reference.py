"""参考排序：完全独立于字节编码，按 schema 声明逐列做 Python 侧比较。

仅用于测试核对——生产扫描路径不允许使用它。
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any

from .codec import _normalize_value  # 复用仅为了把 hex 文本转成 bytes


def _compare_scalar(col: dict, a: Any, b: Any) -> int:
    """单列比较，返回 -1/0/1，严格遵循 order 与 nulls 声明。"""
    if a is None and b is None:
        return 0
    if a is None:
        # NULLS FIRST：NULL 在前（更小）；NULLS LAST：NULL 在后（更大）。
        return -1 if col["nulls"] == "first" else 1
    if b is None:
        return 1 if col["nulls"] == "first" else -1

    if col["type"] == "int64":
        va, vb = a, b
    elif col["type"] == "text":
        # UTF-8 二进制顺序，不做 locale / Unicode 归一化。
        va, vb = a.encode("utf-8"), b.encode("utf-8")
    else:
        va, vb = a, b  # bytes 直接按字节比较

    if va < vb:
        cmpv = -1
    elif va > vb:
        cmpv = 1
    else:
        cmpv = 0
    return -cmpv if col["order"] == "desc" else cmpv


def _row_key_values(schema: list[dict], row: dict[str, Any]) -> list[Any]:
    out = []
    for col in schema:
        v = _normalize_value(col, row[col["name"]])
        out.append(v)
    return out


def reference_sort(
    schema: list[dict],
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按声明逐列比较，业务键完全相同时按 row_id 字节序决胜。"""

    def cmp(row_a: dict[str, Any], row_b: dict[str, Any]) -> int:
        va = _row_key_values(schema, row_a)
        vb = _row_key_values(schema, row_b)
        for col, a, b in zip(schema, va, vb):
            c = _compare_scalar(col, a, b)
            if c != 0:
                return c
        ra, rb = row_a["row_id"].encode("utf-8"), row_b["row_id"].encode("utf-8")
        return -1 if ra < rb else (1 if ra > rb else 0)

    return sorted(rows, key=cmp_to_key(cmp))
