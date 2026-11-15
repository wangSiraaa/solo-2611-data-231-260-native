"""只做行级处理的稳定算子，不含任何排序或哈希聚合。

- filter: 固定谓词、不重排行；
- project: 按原行顺序保留列并改名，不能重排；
- concat: 按输入顺序首尾相接；
- stream_group: 消费“同键连续”的有序输入，逐组产出 COUNT(*) / SUM。
"""

from __future__ import annotations

from typing import Callable, Iterator, Sequence

from .errors import ExecutionError, PlanError
from .ordering import compare_rows, key_tuple, required_group_specs, same_key

_FILTER_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "is_null", "not_null")


def build_filter_predicate(node: dict) -> Callable[[dict], bool]:
    """把计划里的固定 filter 描述编译成谓词。

    支持 {"field": "amount", "ge": 15} 以及 is_null / not_null。
    不支持任意表达式；未知谓词直接拒绝。
    """
    field = node.get("field")
    if not isinstance(field, str) or not field:
        raise PlanError(f"filter 节点 {node.get('id')!r} 缺少 field")
    ops = [key for key in node if key in _FILTER_OPS]
    if len(ops) != 1:
        raise PlanError(
            f"filter 节点 {node.get('id')!r} 必须且只能包含一个谓词，"
            f"实际为 {ops}"
        )
    op = ops[0]
    operand = node[op]

    def predicate(row: dict) -> bool:
        value = row.get(field)
        if op == "is_null":
            return value is None
        if op == "not_null":
            return value is not None
        # SQL 三值逻辑：任一边为 NULL，比较结果为 unknown，即过滤掉。
        if value is None or operand is None:
            return False
        try:
            if op == "eq":
                return value == operand
            if op == "ne":
                return value != operand
            if op == "gt":
                return value > operand
            if op == "ge":
                return value >= operand
            if op == "lt":
                return value < operand
            return value <= operand
        except TypeError as exc:
            raise ExecutionError(
                f"filter 字段 {field} 上的值不可比较: {value!r} 与 {operand!r}"
            ) from exc

    return predicate


def apply_filter(rows: Sequence[dict], predicate: Callable[[dict], bool]) -> list[dict]:
    """稳定过滤：保留行的原有相对次序，不新建排序。"""
    return [row for row in rows if predicate(row)]


def apply_project(
    rows: Sequence[dict],
    input_schema: dict[str, str],
    mapping: dict[str, str],
    node_id: str,
) -> tuple[list[dict], dict[str, str]]:
    """按 mapping {输出列: 来源列} 改名/删列。

    输出列顺序 = 来源列在输入 schema 中的出现顺序（即原行顺序），
    mapping 键自身的书写顺序不决定输出顺序。
    """
    if not mapping:
        raise PlanError(f"project 节点 {node_id!r} 的 mapping 不能为空")
    source_to_output: dict[str, str] = {}
    for output_name, source_name in mapping.items():
        if not isinstance(output_name, str) or not isinstance(source_name, str):
            raise PlanError(f"project 节点 {node_id!r} 的 mapping 键值必须是字符串")
        if source_name not in input_schema:
            raise PlanError(
                f"project 节点 {node_id!r} 来源列 {source_name!r} 不在输入 schema 中"
            )
        if source_name in source_to_output:
            raise PlanError(
                f"project 节点 {node_id!r} 的来源列 {source_name} 被映射了多次"
            )
        source_to_output[source_name] = output_name
    if len(set(source_to_output.values())) != len(source_to_output):
        raise PlanError(f"project 节点 {node_id!r} 存在重复输出列名")

    ordered_sources = [c for c in input_schema if c in source_to_output]
    output_schema = {
        source_to_output[source_name]: input_schema[source_name]
        for source_name in ordered_sources
    }
    projected: list[dict] = []
    for row in rows:
        projected.append(
            {
                source_to_output[source_name]: row[source_name]
                for source_name in ordered_sources
            }
        )
    return projected, output_schema


def concat_rows(parts: Sequence[Sequence[dict]]) -> list[dict]:
    """顺序连接：不做去重、不做排序。"""
    merged: list[dict] = []
    for part in parts:
        merged.extend(part)
    return merged


def stream_groups(
    rows: Sequence[dict], keys: Sequence[str], sum_field: str
) -> Iterator[dict]:
    """按连续相等键流式分组，绝不使用哈希表聚合。

    调用方必须保证 rows 已按 keys 的 ASC + NULLS FIRST 前缀有序；
    这里再次逐相邻行核验，若同键行被拆开（中间插入了别的键）直接报错，
    避免错误的前置顺序被悄悄掩盖。
    """
    specs = required_group_specs(keys)
    if not rows:
        return

    previous = rows[0]
    group_count = 1
    sum_total: int | None = rows[0].get(sum_field)

    def emit(count: int, total: int | None) -> dict:
        out = dict(zip(keys, key_tuple(previous, keys)))
        out["count"] = count
        out["sum"] = total
        return out

    for index in range(1, len(rows)):
        current = rows[index]
        # 物理顺序守卫：ASC NULLS FIRST 下不允许倒退。
        cmp_result = compare_rows(previous, current, specs)
        if cmp_result > 0:
            raise ExecutionError(
                f"stream_group 输入在键 {list(keys)} 上未保持 ASC/NULLS FIRST 有序: "
                f"{previous!r} -> {current!r}"
            )
        if same_key(previous, current, keys):
            group_count += 1
            value = current.get(sum_field)
            if value is not None:
                sum_total = value if sum_total is None else sum_total + value
        else:
            # cmp_result 必为 < 0（相等已被 same_key 覆盖），即新组开始。
            yield emit(group_count, sum_total)
            group_count = 1
            sum_total = current.get(sum_field)
            previous = current
    yield emit(group_count, sum_total)


def group_output_schema(keys: Sequence[str], input_schema: dict[str, str],
                        sum_field: str) -> dict[str, str]:
    sum_type = input_schema[sum_field]
    # 可空整数列的 SUM 仍可能为 NULL，因此统一标 int?；count 恒非空。
    return {**{k: input_schema[k] for k in keys}, "count": "int", "sum": "int?"}
