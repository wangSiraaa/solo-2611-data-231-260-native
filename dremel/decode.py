"""解码器：仅凭 schema 与各叶 {r,d,value?} 列流恢复原始记录。

主解码器采用 Dremel 栈式状态机（每叶独立运行），r/d 语义与编码器严格对称：
- 每条 r=0 条目开启一条根记录，栈重置为单个根 struct 帧；
- 重复级 r 决定弹栈深度：关闭所有“重复级高于 r”的重复单元帧
  （外层 list 帧 r_level=1，元素 struct 与之同生共死，内层 list 帧 r_level=2）；
- 定义级 d 决定沿层级计划向下打开到哪一层：optional 缺失写 null、空 list 写 []、
  null 元素停在 repeated 层、到达叶最大定义级写 value；
- 弹栈后保留的 list 帧复用其容器，下降阶段向它追加新元素；新打开的 list 帧新建容器。

随后多叶结果严格合并：记录数、共享重复祖先的形状（list 长度、null/空/缺失边界）
必须一致，任何矛盾都拒绝，不猜测、不补值。

栈式机与编码递归是两套独立实现；reference.py 另有独立递归参考器做交叉核对。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .planner import LeafPlan, Layer, compile_plans
from .schema import SchemaError, load_schema, validate_and_normalize

_MISSING = object()


class DecodeError(ValueError):
    """列流不合法或列间矛盾，无法反解。"""


# --------------------------------------------------------------------------- #
# 条目形状校验（全局，先于状态机执行）
# --------------------------------------------------------------------------- #
def validate_entry(entry: Any, plan: LeafPlan, path: tuple[str, ...], index: int) -> dict:
    where = f"叶列 {'.'.join(path)} 第 {index} 条"
    if not isinstance(entry, dict):
        raise DecodeError(f"{where}：条目必须是 JSON object")
    if set(entry) - {"r", "d", "value"}:
        raise DecodeError(f"{where}：含非法键 {sorted(set(entry) - {'r', 'd', 'value'})}")
    r, d = entry.get("r"), entry.get("d")
    if not isinstance(r, int) or isinstance(r, bool) or r < 0:
        raise DecodeError(f"{where}：r 必须是非负整数，得到 {r!r}")
    if not isinstance(d, int) or isinstance(d, bool) or d < 0:
        raise DecodeError(f"{where}：d 必须是非负整数，得到 {d!r}")
    if r > d:
        raise DecodeError(f"{where}：不可能的层级 r={r} > d={d}")
    if r > plan.max_r or d > plan.max_d:
        raise DecodeError(f"{where}：层级超出 max_r={plan.max_r}/max_d={plan.max_d}")
    has_value = "value" in entry
    if d == plan.max_d and not has_value:
        raise DecodeError(f"{where}：到达最大定义级却缺失 value")
    if d < plan.max_d and has_value:
        raise DecodeError(f"{where}：未到达最大定义级的条目不允许携带 value")
    if has_value:
        vtype = plan.layers[-1].value_type
        if vtype == "int" and (isinstance(entry["value"], bool) or not isinstance(entry["value"], int)):
            raise DecodeError(f"{where}：int 叶收到非整数 value={entry['value']!r}")
        if vtype == "string" and not isinstance(entry["value"], str):
            raise DecodeError(f"{where}：string 叶收到非字符串 value={entry['value']!r}")
    return entry


# --------------------------------------------------------------------------- #
# 栈帧
# --------------------------------------------------------------------------- #
@dataclass
class Frame:
    kind: str          # "struct" | "list"
    name: str
    r_level: int       # 该帧所处重复级（根/普通 struct=0；外层 list 及其元素=1；内层 list=2）
    layer_index: int
    container: Any     # struct -> dict；list -> list
    element_struct: bool = False  # 仅 list 的 element struct 为 True，参与 r 弹栈换元素


@dataclass
class StackDecoder:
    plan: LeafPlan
    path: tuple[str, ...]
    entries: list[dict]
    pos: int = 0
    stack: list[Frame] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        raise DecodeError(f"叶列 {'.'.join(self.path)} 第 {self.pos} 条：{msg}")

    def run(self) -> list:
        if not self.entries:
            return []  # 零条记录：空列流合法，返回空列表
        records: list = []
        while self.pos < len(self.entries):
            if self.entries[self.pos]["r"] != 0:
                self.fail(f"根记录边界必须 r=0，得到 r={self.entries[self.pos]['r']}")
            records.append(self._process_record())
        return records

    # ------------------------------------------------------------------ #
    def _process_record(self) -> dict:
        root_obj: dict = {}
        self.stack = [Frame("struct", "<root>", 0, 0, root_obj)]
        while True:
            entry = validate_entry(self.entries[self.pos], self.plan, self.path, self.pos)
            self._process_entry(entry)
            self.pos += 1
            if self.pos >= len(self.entries) or self.entries[self.pos]["r"] == 0:
                break
        return root_obj

    def _process_entry(self, entry: dict) -> None:
        r, d = entry["r"], entry["d"]
        if not self.stack:
            self.fail("处理条目前栈为空，层级不合法")

        # 1) 按 r 弹栈：
        #    - 普通 struct（含根）不参与弹栈；
        #    - 元素 struct 帧：r_level >= r 即关闭（r 命中所属 list 重复级就换新元素）；
        #    - list 帧：r_level > r 才关闭（r 回到该 list 重复级时 list 保留只换元素，
        #      r 回到更外层才整个关闭）。
        cut = len(self.stack)
        for idx, frame in enumerate(self.stack):
            if frame.kind == "struct":
                close = frame.element_struct and frame.r_level >= r
            else:
                close = frame.r_level > r
            if close:
                cut = idx
                break
        del self.stack[cut:]
        if not self.stack:
            self.fail("r 弹栈后栈为空，层级不合法")

        # 2) 下降
        top = self.stack[-1]
        top_layer = self.plan.layers[top.layer_index]
        # 栈顶已是 list 帧且条目进入其 repeated 层：
        # - r=2 内层续值（未弹元素 struct）：往当前元素的内层 list 追加；
        # - r=1 外层新元素（弹掉了元素 struct）：由 _open_element 新建元素 struct。
        if top.kind == "list" and d >= top_layer.d_repeat:
            self._open_element(top_layer, top.layer_index, d, entry)
            return
        self._open_path(top.layer_index + 1, d, entry)

    # ------------------------------------------------------------------ #
    def _open_path(self, i: int, d: int, entry: dict) -> None:
        while i < len(self.plan.layers):
            layer = self.plan.layers[i]
            frame = self.stack[-1]

            if layer.kind == "struct":
                if layer.optional and d == layer.d_before:
                    self._put(frame, layer.name, None)
                    return
                obj: dict = {}
                self._put(frame, layer.name, obj)
                # 元素 struct（list 的 element）与所属 list 同重复级；普通 struct 为 0
                struct_r = self._current_repeat_level(i)
                self.stack.append(Frame("struct", layer.name, struct_r, i, obj))
                i += 1
                continue

            if layer.kind == "list":
                if layer.optional and d == layer.d_before:
                    self._put(frame, layer.name, None)
                    return
                if d == layer.d_present:
                    self._put(frame, layer.name, [])  # 空 list
                    return
                # 非空 list：新建 list 帧并打开当前元素
                lst: list = []
                self._put(frame, layer.name, lst)
                self.stack.append(Frame("list", layer.name, layer.r_level, i, lst))  # type: ignore[arg-type]
                self._open_element(layer, i, d, entry)
                return

            # leaf
            if layer.optional and d == layer.d_before:
                self._put(frame, layer.name, None)
                return
            if d != layer.d_present:
                self.fail(f"叶 {layer.name!r} 的 d={d} 不等于其最大定义级 {layer.d_present}")
            self._put(frame, layer.name, entry["value"])
            return

        self.fail(f"d={d} 未能落到任何层级节点")

    def _current_repeat_level(self, struct_layer_index: int) -> int:
        """元素 struct 帧的重复级取其所属 list 的 r_level；普通 struct 为 0。"""
        for frame in reversed(self.stack):
            if frame.kind == "list" and frame.layer_index < struct_layer_index:
                return frame.r_level
        return 0

    def _open_element(self, list_layer: Layer, list_index: int, d: int, entry: dict) -> None:
        """在已压入的 list 帧上打开当前条目对应的一个元素。"""
        if d == list_layer.d_repeat:
            # null 元素：停在元素 optional 层之前
            if not list_layer.element_optional:
                self.fail(f"d={d} 停在 repeated 层但 element 为 required，不可能为 null 元素")
            self.stack[-1].container.append(None)
            return
        if d < list_layer.d_element:
            self.fail(f"d={d} 落在元素 optional 层的不可能区间")

        elem_layer = self.plan.layers[list_index + 1]
        list_frame = self.stack[-1]

        if elem_layer.kind == "leaf":
            # list 直接包叶：d==d_repeat 为 null（上方已处理），d==max_d 带值
            if d != self.plan.max_d:
                self.fail(f"list 叶元素 d={d} 未达最大定义级 {self.plan.max_d}")
            list_frame.container.append(entry["value"])
            return

        if elem_layer.kind == "struct":
            obj: dict = {}
            list_frame.container.append(obj)
            self.stack.append(
                Frame(
                    "struct",
                    list_layer.name,
                    list_layer.r_level,
                    list_index + 1,
                    obj,
                    element_struct=True,
                )
            )
            self._open_path(list_index + 2, d, entry)
            return

        # element 仍是 list（第二层 list）
        if elem_layer.optional and d == elem_layer.d_before:
            list_frame.container.append(None)
            return
        if d == elem_layer.d_present:
            list_frame.container.append([])
            return
        inner: list = []
        list_frame.container.append(inner)
        self.stack.append(
            Frame("list", list_layer.name, elem_layer.r_level, list_index + 1, inner)  # type: ignore[arg-type]
        )
        self._open_element(elem_layer, list_index + 1, d, entry)

    @staticmethod
    def _put(frame: Frame, name: str, value: Any) -> None:
        if frame.kind == "struct":
            frame.container[name] = value
        else:
            frame.container.append(value)


# --------------------------------------------------------------------------- #
# 多叶合并与整体校验
# --------------------------------------------------------------------------- #
def _merge(a: object, b: object, path: str) -> object:
    """合并两个叶视角片段；结构形状必须完全一致，矛盾即拒绝。"""
    if a is _MISSING:
        return b
    if b is _MISSING:
        return a
    if a is None or b is None:
        if a is not b:
            raise DecodeError(f"共享边界在 {path or '<root>'} 矛盾：一侧 null 一侧非 null")
        return None
    if isinstance(a, dict) or isinstance(b, dict):
        if not (isinstance(a, dict) and isinstance(b, dict)):
            raise DecodeError(f"共享边界在 {path} 矛盾：struct 与非 struct 冲突")
        keys = set(a) | set(b)
        out: dict = {}
        for k in sorted(keys):
            out[k] = _merge(a.get(k, _MISSING), b.get(k, _MISSING), f"{path}.{k}" if path else k)
        return out
    if isinstance(a, list) or isinstance(b, list):
        if not (isinstance(a, list) and isinstance(b, list)):
            raise DecodeError(f"共享边界在 {path} 矛盾：list 与非 list 冲突")
        if len(a) != len(b):
            raise DecodeError(
                f"共享重复祖先 {path} 形状矛盾：list 长度 {len(a)} != {len(b)}"
            )
        return [_merge(x, y, f"{path}[]") for x, y in zip(a, b)]
    if a != b:
        raise DecodeError(f"共享边界在 {path} 矛盾：叶值 {a!r} != {b!r}")
    return a


def load_column_file(path: Path) -> list[dict]:
    entries: list[dict] = []
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise DecodeError(f"{path.name} 第 {line_no} 行不是合法 JSON：{exc}") from exc
    return entries


def decode_columns(schema: dict, columns: dict[tuple[str, ...], list[dict]]) -> list[dict]:
    """主解码入口：schema + {叶路径: 条目列表} -> 规范化记录列表。"""
    schema = load_schema(schema)
    plans = compile_plans(schema)

    if set(columns) != set(plans):
        missing = sorted(".".join(p) for p in plans.keys() - columns.keys())
        extra = sorted(".".join(p) for p in columns.keys() - plans.keys())
        raise DecodeError(f"叶列集合与 schema 不一致，缺失={missing} 多余={extra}")

    record_count: int | None = None
    per_leaf_records: dict[tuple[str, ...], list] = {}
    for path, plan in plans.items():
        entries = columns[path]
        for idx, entry in enumerate(entries):
            validate_entry(entry, plan, path, idx)
        zeros = sum(1 for e in entries if e["r"] == 0)
        if record_count is None:
            record_count = zeros
        elif zeros != record_count:
            raise DecodeError(
                f"叶列 {'.'.join(path)} 根记录数 {zeros} 与其他列 {record_count} 不一致"
            )
        per_leaf_records[path] = StackDecoder(plan, path, entries).run()
        if len(per_leaf_records[path]) != record_count:
            raise DecodeError(
                f"叶列 {'.'.join(path)} 状态机产出 {len(per_leaf_records[path])} 条记录，应为 {record_count}"
            )
    assert record_count is not None

    records: list[dict] = []
    for row_index in range(record_count):
        merged: dict = {}
        for path in plans:
            fragment = per_leaf_records[path][row_index]
            merged = _merge(merged, fragment, "")
        try:
            normalized = validate_and_normalize(schema, merged, name="<root>")
        except (SchemaError, ValueError) as exc:
            raise DecodeError(f"第 {row_index} 条记录反解结果无法通过 schema 校验：{exc}") from exc
        records.append(normalized)
    return records


def decode_directory(out_dir: str | Path) -> list[dict]:
    """从 encode 写出的目录直接反解。"""
    from .metadata import META_FILE, RECORD_COUNT_FILE  # 局部导入避免环

    out = Path(out_dir)
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    count_doc = json.loads((out / RECORD_COUNT_FILE).read_text(encoding="utf-8"))
    schema = meta["schema"]
    expected_count = count_doc["record_count"]

    columns: dict[tuple[str, ...], list[dict]] = {}
    for col in meta["columns"]:
        path = tuple(col["leaf_path"])
        columns[path] = load_column_file(out / col["column_file"])

    records = decode_columns(schema, columns)
    if len(records) != expected_count:
        raise DecodeError(
            f"记录数文件声明 {expected_count} 条，列流实际反解出 {len(records)} 条"
        )
    return records
