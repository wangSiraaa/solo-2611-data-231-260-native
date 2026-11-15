"""排序键描述、NULL 感知比较与稳定排序原语。

排序描述符 specs 形如::

    [{"field": "region", "direction": "asc", "nulls": "first"},
     {"field": "tick",   "direction": "desc", "nulls": "last"}]

- direction 只取 "asc" / "desc"，nulls 只取 "first" / "last"。
- NULL 的位置完全由 nulls 规则决定，desc 不会连带把 NULL 翻到另一侧
  （NULLS FIRST/LAST 是独立语义）。
- 非 NULL 值按 Python 原生值比较；同层键类型不一致直接报错，不隐式转换。
"""

from __future__ import annotations

from functools import cmp_to_key
from typing import Any, Iterable, Sequence

from .errors import OrderingError

ASC = "asc"
DESC = "desc"
NULLS_FIRST = "first"
NULLS_LAST = "last"


def validate_spec(spec: dict) -> dict:
    """校验并规范化一个排序键描述。"""
    if not isinstance(spec, dict) or "field" not in spec:
        raise OrderingError(f"非法排序键描述: {spec!r}")
    field = spec["field"]
    if not isinstance(field, str) or not field:
        raise OrderingError(f"排序键 field 必须是非空字符串: {spec!r}")
    direction = spec.get("direction", ASC)
    if direction not in (ASC, DESC):
        raise OrderingError(f"排序键 {field} 的 direction 非法: {direction!r}")
    nulls = spec.get("nulls", NULLS_FIRST)
    if nulls not in (NULLS_FIRST, NULLS_LAST):
        raise OrderingError(f"排序键 {field} 的 nulls 非法: {nulls!r}")
    return {"field": field, "direction": direction, "nulls": nulls}


def normalize_specs(specs: Sequence[dict]) -> list[dict]:
    if not isinstance(specs, list):
        raise OrderingError(f"排序键列表必须是数组: {specs!r}")
    return [validate_spec(s) for s in specs]


def required_group_specs(keys: Sequence[str]) -> list[dict]:
    """stream_group 要求的输入前缀：按键声明顺序 ASC + NULLS FIRST。"""
    return [{"field": k, "direction": ASC, "nulls": NULLS_FIRST} for k in keys]


def same_specs(left: Sequence[dict], right: Sequence[dict]) -> bool:
    """两个描述符是否逐键完全一致（field/direction/nulls 都相同）。"""
    return list(left) == list(right)


def _value(row: dict, field: str) -> Any:
    # 行中缺列等价于 NULL，和显式 null 走同一条 NULL 语义。
    return row.get(field)


def compare_rows(left: dict, right: dict, specs: Sequence[dict]) -> int:
    """按 specs 比较两行，返回 -1 / 0 / 1。"""
    for spec in specs:
        field = spec["field"]
        a = _value(left, field)
        b = _value(right, field)
        a_null = a is None
        b_null = b is None
        if a_null or b_null:
            if a_null and b_null:
                continue
            # NULL 位置独立于 asc/desc。
            if spec["nulls"] == NULLS_FIRST:
                return -1 if a_null else 1
            return 1 if a_null else -1
        try:
            if a == b:
                continue
            less = a < b
        except TypeError as exc:  # 例如 int 与 str 混排
            raise OrderingError(
                f"排序键 {field} 上的值类型不可比较: {a!r} 与 {b!r}"
            ) from exc
        cmp_result = -1 if less else 1
        if spec["direction"] == DESC:
            cmp_result = -cmp_result
        return cmp_result
    return 0


class _Key:
    """让 sorted 可复用 compare_rows 的比较器包装。"""

    __slots__ = ("row", "specs")

    def __init__(self, row: dict, specs: Sequence[dict]):
        self.row = row
        self.specs = specs

    def __lt__(self, other: "_Key") -> bool:
        return compare_rows(self.row, other.row, self.specs) < 0


def stable_sort(rows: Sequence[dict], specs: Sequence[dict]) -> list[dict]:
    """Python 稳定排序：相等键保持原相对次序，返回新列表。"""
    if not specs:
        return list(rows)
    return sorted(rows, key=cmp_to_key(lambda a, b: compare_rows(a, b, specs)))


def verify_sorted(
    rows: Sequence[dict], specs: Sequence[dict], context: str
) -> None:
    """核验整段数据在 specs 下全程不倒退，违反即抛 OrderingError。

    相等允许通过——这正是 stream_group 所需的“同键连续”性质。
    """
    for index in range(1, len(rows)):
        cmp_result = compare_rows(rows[index - 1], rows[index], specs)
        if cmp_result > 0:
            raise OrderingError(
                f"{context}: 第 {index - 1} 行到第 {index} 行在排序键 "
                f"{[s['field'] for s in specs]} 下倒退 "
                f"({rows[index - 1]!r} -> {rows[index]!r})"
            )


def satisfies_prefix(
    actual: Sequence[dict], required: Sequence[dict]
) -> tuple[bool, str]:
    """actual 是否能证明 required 是其排序前缀。

    - 前缀逐键要求 field/direction/nulls 全部一致；
    - actual 允许带额外的后续键（更细的次序对粗前缀仍是充分证据）；
    - actual 比 required 短、或第一处不一致即失效，并给出原因。
    """
    if len(actual) < len(required):
        missing = [s["field"] for s in required[len(actual):]]
        return False, f"可证明次序缺少排序键 {missing}"
    for index, req in enumerate(required):
        got = actual[index]
        if got["field"] != req["field"]:
            return False, (
                f"第 {index + 1} 个排序键字段为 {got['field']!r}，"
                f"要求 {req['field']!r}"
            )
        if got["direction"] != req["direction"]:
            return False, (
                f"键 {req['field']} 方向为 {got['direction']}，"
                f"要求 {req['direction']}"
            )
        if got["nulls"] != req["nulls"]:
            return False, (
                f"键 {req['field']} NULL 规则为 {got['nulls']}，"
                f"要求 {req['nulls']}"
            )
    return True, "输入排序前缀与要求逐键一致"


def key_tuple(row: dict, keys: Iterable[str]) -> tuple:
    return tuple(row.get(k) for k in keys)


def same_key(left: dict, right: dict, keys: Sequence[str]) -> bool:
    return key_tuple(left, keys) == key_tuple(right, keys)
