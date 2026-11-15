"""Schema 加载与严格校验。

支持的最小子集：
- struct：含 fields 映射；根必须是 required struct
- list：含一个 element，element 自带 repetition（optional/required）；嵌套最多两层 list
- 叶节点：int / string
任意节点都可以 optional（根除外，根为 required struct）。
"""

from __future__ import annotations

from typing import Any

_ALLOWED_TYPES = ("struct", "list", "int", "string")
_INT_MIN = -(2**63)
_INT_MAX = 2**63 - 1


class SchemaError(ValueError):
    """schema 本身不合法。"""


class RowValidationError(ValueError):
    """输入记录不符合 schema（含必填缺失、额外字段、类型不符、null 出现在 required）。"""


def load_schema(raw: Any) -> dict:
    """加载并递归校验 schema，返回原样 dict（校验后即可信）。"""
    if not isinstance(raw, dict):
        raise SchemaError("schema 顶层必须是 JSON object")
    _validate_node(raw, name="<root>", list_depth=0, is_root=True)
    if raw["type"] != "struct" or raw.get("optional") is not False:
        raise SchemaError("根节点必须是 required struct")
    return raw


def _validate_node(node: Any, *, name: str, list_depth: int, is_root: bool = False) -> None:
    if not isinstance(node, dict):
        raise SchemaError(f"节点 {name!r} 必须是 object")
    t = node.get("type")
    if t not in _ALLOWED_TYPES:
        raise SchemaError(f"节点 {name!r} 的 type 必须是 {_ALLOWED_TYPES} 之一，得到 {t!r}")
    if "optional" not in node or not isinstance(node["optional"], bool):
        raise SchemaError(f"节点 {name!r} 必须显式声明布尔型 optional")
    if not is_root and node["optional"] is False and t == "struct" and list_depth == 0:
        # 允许 required struct（如 list 的 element struct），这里无需限制；
        # 根 struct 已在 load_schema 单独处理。
        pass

    if t == "struct":
        fields = node.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise SchemaError(f"struct {name!r} 必须含非空 fields")
        for fname, child in fields.items():
            if not isinstance(fname, str) or not fname:
                raise SchemaError(f"struct {name!r} 存在非法字段名 {fname!r}")
            _validate_node(child, name=f"{name}.{fname}", list_depth=list_depth)
    elif t == "list":
        if list_depth >= 2:
            raise SchemaError(f"list {name!r} 嵌套超过两层（本实现最多两层 list）")
        element = node.get("element")
        if not isinstance(element, dict):
            raise SchemaError(f"list {name!r} 必须含 element 节点")
        _validate_node(element, name=f"{name}[].element", list_depth=list_depth + 1)
    else:  # 叶节点
        extra = set(node) - {"type", "optional"}
        if extra:
            raise SchemaError(f"叶节点 {name!r} 不允许额外键 {sorted(extra)}")


# --------------------------------------------------------------------------- #
# 输入记录校验 + 规范化：缺失 optional 与显式 null 统一规范为 None；
# required 出现 null / 缺失、额外字段、类型不符一律拒绝。
# --------------------------------------------------------------------------- #


def validate_and_normalize(node: dict, value: Any, *, name: str) -> Any:
    """按 schema 校验一条记录片段，返回规范化后的片段（optional 缺失补 None）。"""
    optional = node["optional"]
    t = node["type"]

    if value is None:
        if optional:
            return None
        raise RowValidationError(f"required 节点 {name!r} 不允许为 null")

    if t == "struct":
        return _normalize_struct(node, value, name)
    if t == "list":
        return _normalize_list(node, value, name)
    if t == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise RowValidationError(f"int 节点 {name!r} 需要整数，得到 {value!r}")
        if not _INT_MIN <= value <= _INT_MAX:
            raise RowValidationError(f"int 节点 {name!r} 的值 {value} 超出 64 位有符号范围")
        return value
    # string
    if not isinstance(value, str):
        raise RowValidationError(f"string 节点 {name!r} 需要字符串，得到 {value!r}")
    return value


def _normalize_struct(node: dict, value: Any, name: str) -> dict:
    if not isinstance(value, dict):
        raise RowValidationError(f"struct 节点 {name!r} 需要 object，得到 {type(value).__name__}")
    fields = node["fields"]
    given = set(value)
    allowed = set(fields)
    unknown = given - allowed
    if unknown:
        raise RowValidationError(f"struct {name!r} 出现 schema 外字段 {sorted(unknown)}")
    out: dict[str, Any] = {}
    for fname, child in fields.items():
        if fname not in value:
            if not child["optional"]:
                raise RowValidationError(f"required 字段 {name}.{fname} 缺失")
            out[fname] = None  # 缺失 optional 与显式 null 同规范
        else:
            out[fname] = validate_and_normalize(child, value[fname], name=f"{name}.{fname}")
    return out


def _normalize_list(node: dict, value: Any, name: str) -> list:
    if not isinstance(value, list):
        raise RowValidationError(f"list 节点 {name!r} 需要数组，得到 {type(value).__name__}")
    element = node["element"]
    return [
        validate_and_normalize(element, item, name=f"{name}[]")
        for item in value
    ]


def leaf_paths(node: dict, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """收集全部叶路径（按字段名字典序稳定输出）。"""
    t = node["type"]
    if t in ("int", "string"):
        return [prefix]
    if t == "struct":
        out: list[tuple[str, ...]] = []
        for fname in sorted(node["fields"]):
            out.extend(leaf_paths(node["fields"][fname], prefix + (fname,)))
        return out
    # list：element 不占字段名，路径不额外加名
    return leaf_paths(node["element"], prefix)
