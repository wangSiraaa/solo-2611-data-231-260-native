"""独立参考解码器（仅用于交叉核对，不参与主解码路径）。

与 decode.StackDecoder 的 Frame 对象栈刻意不同，本模块用“容器句柄表”做单次
事件扫描：每个非空 list 打开时分配一个 list 句柄，每个元素 struct 分配一个
struct 句柄，句柄按其重复级登记；每条事件先按 r 关闭更深重复级的句柄，再按 d
逐层寻址，复用仍打开的句柄、为新元素分配新句柄。无 Frame 类、无弹栈 while，
控制流与主解码器相互独立，可互相捕获边界 bug。纯标准库，不依赖列式库。
"""

from __future__ import annotations

from .planner import LeafPlan
from .decode import DecodeError, validate_entry


def reference_decode(plan: LeafPlan, entries: list[dict]) -> list[dict]:
    for idx, e in enumerate(entries):
        validate_entry(e, plan, path=plan.path, index=idx)
    if not entries:
        return []

    records: list[dict] = []
    i = 0
    while i < len(entries):
        if entries[i]["r"] != 0:
            raise DecodeError(
                f"叶列 {'.'.join(plan.path)} 第 {i} 条：根记录首条必须 r=0"
            )
        j = i + 1
        while j < len(entries) and entries[j]["r"] != 0:
            j += 1
        records.append(_decode_record(plan, entries[i:j]))
        i = j
    return records


def _owner_repeat(plan: LeafPlan, struct_layer_index: int) -> int:
    """元素 struct 所属 list 的重复级（其上最近 list 层的 r_level，否则 0）。"""
    owner = 0
    for layer in plan.layers[:struct_layer_index]:
        if layer.kind == "list":
            owner = layer.r_level  # type: ignore[assignment]
    return owner


def _decode_record(plan: LeafPlan, events: list[dict]) -> dict:
    root: dict = {}
    # 句柄表：list 句柄按重复级登记（每个重复级至多一个当前打开 list）；
    # struct 句柄按层级索引登记，随其 owner list 关闭而失效。
    open_list: dict[int, list] = {}
    open_struct: dict[int, dict] = {}

    for ev in events:
        r, d = ev["r"], ev["d"]
        # 1) r 截断：关闭重复级 > r 的 list 句柄及其元素 struct 句柄
        for level in [lv for lv in open_list if lv > r]:
            open_list.pop(level)
        for key in [k for k, obj in open_struct.items() if _owner_repeat(plan, k) > r]:
            open_struct.pop(key)
        # 2) d 寻址
        _descend(plan, ev, r, open_list, open_struct, root)

    return root


def _descend(
    plan: LeafPlan,
    ev: dict,
    r: int,
    open_list: dict[int, list],
    open_struct: dict[int, dict],
    root: dict,
) -> None:
    d = ev["d"]
    host: dict = root
    li = 0
    while li < len(plan.layers):
        layer = plan.layers[li]

        if layer.kind == "struct":
            if li == 0:
                li += 1  # 根 struct 即 root，不加键名
                continue
            if layer.optional and d == layer.d_before:
                host[layer.name] = None
                return
            owner = _owner_repeat(plan, li)
            owner_list = open_list.get(owner)
            # 仅当存在已打开的 owner list 时，本 struct 才是 list 的 element struct
            # （按 list 元素处理，append 到 list，不按字段名挂 host）；
            # 否则是普通 struct（如 meta），按字段名挂 host 并复用缓存对象。
            if owner_list is None:
                obj = open_struct.get(li)
                if obj is None:
                    obj = {}
                    open_struct[li] = obj
                host[layer.name] = obj
                host = obj
                li += 1
                continue
            # element struct
            current = open_struct.get(li)
            new_element = current is None or r == owner
            if new_element:
                obj: dict = {}
                owner_list.append(obj)
                open_struct[li] = obj
            host = open_struct[li]
            li += 1
            continue

        if layer.kind == "list":
            if layer.optional and d == layer.d_before:
                host[layer.name] = None
                return
            if d == layer.d_present:
                host[layer.name] = []
                return
            level = layer.r_level
            assert level is not None
            # 句柄仍打开则复用（同 list 续值），否则新建并登记（新 list/新元素）
            if level not in open_list or host.get(layer.name) is not open_list[level]:
                lst: list = []
                host[layer.name] = lst
                open_list[level] = lst
            lst = open_list[level]

            if d == layer.d_repeat:  # null 元素
                if not layer.element_optional:
                    raise DecodeError(
                        f"叶列 {'.'.join(plan.path)}：required element 出现 null 元素"
                    )
                lst.append(None)
                return

            elem = plan.layers[li + 1]
            if elem.kind == "leaf":
                if d != plan.max_d:
                    raise DecodeError(
                        f"叶列 {'.'.join(plan.path)}：list 叶元素 d={d} 未达 max_d"
                    )
                lst.append(ev["value"])
                return
            if elem.kind == "struct":
                # 元素 struct 的创建/append 由下一轮 struct 分支统一处理
                li += 1
                continue
            # element 仍是 list（第二层）：进入内层 list 层
            li += 1
            continue

        # leaf
        if layer.optional and d == layer.d_before:
            host[layer.name] = None
            return
        if d != layer.d_present:
            raise DecodeError(
                f"叶列 {'.'.join(plan.path)}：叶 d={d} 不等于 max_d {layer.d_present}"
            )
        host[layer.name] = ev["value"]
        return

    raise DecodeError(f"叶列 {'.'.join(plan.path)}：d={d} 未能落到任何层级节点")
