"""编码器：规范化记录 -> 每个叶路径一条独立的 {r,d,value?} JSONL 列流。

关键不变量：
- 每个根记录在每个叶列至少留下一个条目（即使整条路径都缺失，也只发一个无 value 标记）；
- 只有 d == 叶最大定义级的条目携带 value；
- 条目的 r = 该条目被发出时“仍然打开”的最深 list 重复层编号；
- 一个 list 第一个元素沿用进入 list 时的 r，后续元素取该 list 自身的 r_level。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .planner import LeafPlan, compile_plans
from .schema import load_schema, validate_and_normalize

COLUMN_SUFFIX = ".jsonl"
_MARK = object()


class EncodeError(ValueError):
    """编码阶段发现的数据不合法（理论上规范化后不应再触发）。"""


class ColumnEncoder:
    def __init__(self, schema: dict):
        self.schema = load_schema(schema)
        self.plans = compile_plans(self.schema)
        self.columns: dict[tuple[str, ...], list[dict]] = {
            path: [] for path in self.plans
        }

    # ------------------------------------------------------------------ #
    # 公共入口
    # ------------------------------------------------------------------ #
    def add_row(self, raw_row: Any) -> dict:
        """校验、规范化并编码一条根记录，返回规范化后的记录。"""
        row = validate_and_normalize(self.schema, raw_row, name="<root>")
        # 根是 required struct：直接逐字段编码，d 从 0 开始、r=0
        self._encode_struct_fields(self.schema, row, d_in=0, r_in=0, prefix=())
        return row

    def encode_all(self, rows: list[Any]) -> list[dict]:
        return [self.add_row(row) for row in rows]

    def write(self, out_dir: str | Path) -> None:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        for path, entries in self.columns.items():
            fp = out / ("__".join(path) + COLUMN_SUFFIX)
            with fp.open("w", encoding="utf-8") as f:
                for entry in entries:
                    f.write(
                        json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )

    # ------------------------------------------------------------------ #
    # 内部递归
    # ------------------------------------------------------------------ #
    def _emit(self, path: tuple[str, ...], r: int, d: int, plan: LeafPlan, value: Any = _MARK) -> None:
        entry: dict[str, Any] = {"r": r, "d": d}
        if value is not _MARK:
            if d != plan.max_d:
                raise EncodeError(f"叶 {'.'.join(path)} 在 d={d}（非 max_d={plan.max_d}）携带 value")
            entry["value"] = value
        elif d == plan.max_d:
            raise EncodeError(f"叶 {'.'.join(path)} 到达 max_d 却缺少 value")
        self.columns[path].append(entry)

    def _emit_marker_to_descendants(
        self, node: dict, r: int, d_stop: int, prefix: tuple[str, ...]
    ) -> None:
        """某结构在定义级 d_stop 终止时，向其全部后代叶各发一个无 value 标记。"""
        t = node["type"]
        if t in ("int", "string"):
            self._emit(prefix, r, d_stop, self.plans[prefix])
            return
        if t == "struct":
            for fname, child in node["fields"].items():
                self._emit_marker_to_descendants(child, r, d_stop, prefix + (fname,))
            return
        # list：标记在 list 边界终止，后代叶都挂在 element 之下（路径不新增名字）
        self._emit_marker_to_descendants(node["element"], r, d_stop, prefix)

    def _encode_field(
        self,
        node: dict,
        value: Any,
        *,
        prefix: tuple[str, ...],
        d_in: int,
        r_in: int,
        element_optional_consumed: bool,
    ) -> None:
        optional = node["optional"] and not element_optional_consumed
        d_present = d_in + (1 if optional else 0)
        t = node["type"]

        # optional 缺失 / 显式 null：停在进入该 optional 层之前（d_in）
        if value is None:
            if not optional:
                raise EncodeError(f"required 节点 {'.'.join(prefix) or '<root>'} 出现 null")
            self._emit_marker_to_descendants(node, r_in, d_in, prefix)
            return

        if t == "struct":
            self._encode_struct_fields(node, value, d_in=d_present, r_in=r_in, prefix=prefix)
        elif t == "list":
            self._encode_list(node, value, prefix=prefix, d_present=d_present, r_in=r_in)
        else:
            # 叶节点存在：必达 max_d 且携带 value
            self._emit(prefix, r_in, d_present, self.plans[prefix], value)

    def _encode_struct_fields(
        self, struct_node: dict, value: dict, *, d_in: int, r_in: int, prefix: tuple[str, ...]
    ) -> None:
        for fname, child in struct_node["fields"].items():
            self._encode_field(
                child,
                value[fname],
                prefix=prefix + (fname,),
                d_in=d_in,
                r_in=r_in,
                element_optional_consumed=False,
            )

    def _static_r_level(self, prefix: tuple[str, ...]) -> int:
        """从编译后的叶计划反查某个 list 路径前缀的静态权威重复级。

        沿叶计划的层级序列匹配与 prefix 同名的 list 层；同一叶计划内 list 名不重复，
        取首个匹配即可。planner 已静态标定，嵌套内层 list 不会被首元素沿用的低 r_in 污染。
        """
        for plan in self.plans.values():
            for layer in plan.layers:
                if layer.kind == "list" and layer.name == prefix[-1]:
                    return layer.r_level  # type: ignore[return-value]
        raise EncodeError(f"无法确定 list 路径 {prefix} 的静态重复级")

    def _encode_list(
        self, node: dict, value: list, *, prefix: tuple[str, ...], d_present: int, r_in: int
    ) -> None:
        # 静态权威 r_level 来自 planner（嵌套内层 list 不会被首元素沿用的低 r_in 污染）
        r_level = self._static_r_level(prefix)
        d_repeat = d_present + 1  # repeated element 层
        elem = node["element"]
        d_element = d_repeat + (1 if elem["optional"] else 0)

        if not value:
            # 空 list：停在进入 repeated 层之前（d_present）
            self._emit_marker_to_descendants(elem, r_in, d_present, prefix)
            return

        for index, item in enumerate(value):
            # 任务 r 定义：条目相对前一条“重新开始的最深重复层编号”。
            # list 第一个元素沿用进入该 list 时的 r（在更外层重复层重新开始）；
            # 其后元素在该 list 自身重复层重新开始，取该 list 的 r_level。
            # 嵌套内层 list 递归时 r_in 已携带外层 r_level，故内层后续元素能正确取到 2。
            item_r = r_in if index == 0 else r_level
            if item is None:
                if not elem["optional"]:
                    raise EncodeError(f"list {'.'.join(prefix)} 出现 null 元素但 element 为 required")
                # null 元素：停在该元素 optional 层之前（d_repeat）
                self._emit_marker_to_descendants(elem, item_r, d_repeat, prefix)
                continue
            # element 的 optional 已折算进 d_element，递归时标记已消费，避免重复 +1
            self._encode_field(
                elem,
                item,
                prefix=prefix,
                d_in=d_element,
                r_in=item_r,
                element_optional_consumed=elem["optional"],
            )
