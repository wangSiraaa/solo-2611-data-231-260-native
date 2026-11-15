"""把 schema 编译为每个叶路径的 r/d 层级计划。

定义级 d 沿根到叶路径累计：
- 每个 optional 节点贡献 1；
- 每个 list 的 repeated element 层固定贡献 1（无论 element 是否 optional）；
- element 自身的 optional 再贡献 1（在 list 边界处计入，进入 element 后不再重复累计）。

重复级 r：根记录为 0；每进入一层 list，该层元素的 r 编号 +1（最外层=1，内层=2）。
编码与解码共用同一份编译结果，阈值口径不会漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

from .schema import leaf_paths


@dataclass
class Layer:
    """叶路径上的一个结构层（struct / list / leaf）。"""

    kind: str  # "struct" | "list" | "leaf"
    name: str
    optional: bool
    d_before: int  # 进入该层之前的定义级
    d_present: int  # 该层自身“存在”时的定义级
    # ---- list 专用 ----
    r_level: int | None = None
    d_repeat: int | None = None  # 进入 repeated element 层后的定义级（元素 optional 之前）
    element_optional: bool | None = None
    d_element: int | None = None  # 元素自身非 null 时的定义级
    # ---- leaf 专用 ----
    value_type: str | None = None


@dataclass
class LeafPlan:
    path: tuple[str, ...]
    layers: tuple[Layer, ...]
    max_d: int
    max_r: int
    # 处理每层时所处的最深重复层编号（与 layers 等长）
    r_stack_at_layer: tuple[int, ...]


def compile_plans(schema: dict) -> dict[tuple[str, ...], LeafPlan]:
    plans: dict[tuple[str, ...], LeafPlan] = {}
    for path in leaf_paths(schema):
        plans[path] = _compile_path(schema, list(path))
    return plans


def _compile_path(root: dict, original_path: list[str]) -> LeafPlan:
    path = list(original_path)
    layers: list[Layer] = []
    r_at: list[int] = []

    def walk(node: dict, name: str, d_in: int, r_in: int, element_optional_consumed: bool) -> tuple[int, int]:
        # element 的 optional 在所属 list 层已计入 d_element，递归 element 时不再重复 +1
        optional = node["optional"] and not element_optional_consumed
        d_present = d_in + (1 if optional else 0)
        t = node["type"]

        if t == "struct":
            layers.append(Layer("struct", name, optional, d_in, d_present))
            r_at.append(r_in)
            child_name = path.pop(0)
            child = node["fields"][child_name]
            return walk(child, child_name, d_present, r_in, False)

        if t == "list":
            r_level = r_in + 1
            # d_present 是 list 自身存在（含空 list）的级别：optional 已在 d_present 累计，
            # 这里不重复加；repeated element 层固定再 +1。
            d_repeat = d_present + 1
            elem = node["element"]
            d_element = d_repeat + (1 if elem["optional"] else 0)
            layers.append(
                Layer(
                    "list",
                    name,
                    optional,
                    d_in,
                    d_present,  # list 自身存在/空 list 的级别（optional 已在 d_present 内）
                    r_level=r_level,
                    d_repeat=d_repeat,
                    element_optional=elem["optional"],
                    d_element=d_element,
                )
            )
            r_at.append(r_level)
            return walk(elem, name, d_element, r_level, elem["optional"])

        # leaf
        layers.append(Layer("leaf", name, optional, d_in, d_present, value_type=t))
        r_at.append(r_in)
        return d_present, r_in

    max_d, max_r = walk(root, "<root>", 0, 0, False)
    assert not path, f"叶路径编译后仍有未消费字段: {path}"
    return LeafPlan(
        path=tuple(original_path),
        layers=tuple(layers),
        max_d=max_d,
        max_r=max_r,
        r_stack_at_layer=tuple(r_at),
    )
