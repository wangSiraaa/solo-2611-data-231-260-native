"""预检（编译）：AST 结构校验 + 类型定型，产出编译计划。

任何结构/类型错误都在此阶段以 CompileError 拒绝整个作业，
绝不在运行期退化成逐行 NULL。NULL 字面量由上下文定型，无法定型则拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass

from .model import (
    INT64_MAX, INT64_MIN, OPS, TYPE_BOOL, TYPE_INT, TYPE_TEXT,
    CompileError, Node,
)

_BINARY = ("add", "div", "eq", "gt", "and", "or")   # 恰好 2 个子节点（列表）
_UNARY = ("to_int", "not", "is_null")               # 恰好 1 个子节点（单个对象）


@dataclass
class Plan:
    """一个输出表达式的编译结果。"""
    name: str
    root: Node

    def nodes(self) -> list[Node]:
        out: list[Node] = []

        def walk(n: Node) -> None:
            out.append(n)
            for c in n.children:
                walk(c)

        walk(self.root)
        return out

    def to_dict(self) -> dict:
        nodes = []
        for n in self.nodes():
            entry = {
                "path": n.path,
                "op": n.op,
                "type": n.type,
                "nullable": n.nullable,
            }
            if n.op == "col":
                entry["column"] = n.col_name
            elif n.op == "literal":
                entry["value"] = n.literal
            nodes.append(entry)
        return {
            "name": self.name,
            "result_type": self.root.type,
            "nullable": self.root.nullable,
            "nodes": nodes,
        }


def compile_all(expressions: list[dict], schema: dict[str, tuple[str, bool]]) -> dict[str, Plan]:
    plans: dict[str, Plan] = {}
    for item in expressions:
        name = item["name"]
        try:
            root = _parse(item["expr"], "$", schema)
            _infer(root, schema)
            _check_all_typed(root)
        except CompileError as e:
            raise CompileError(f"表达式 {name!r}: {e}") from e
        plans[name] = Plan(name=name, root=root)
    return plans


# ---------------------------------------------------------------- 结构解析

def _parse(raw, path: str, schema) -> Node:
    if not isinstance(raw, dict) or len(raw) != 1:
        raise CompileError(f"{path}: 节点必须是恰含一个 operator 的对象")
    op, body = next(iter(raw.items()))
    if op not in OPS:
        raise CompileError(f"{path}: 未知 operator {op!r}")
    node = Node(op=op, path=path)

    if op == "col":
        if not isinstance(body, str):
            raise CompileError(f"{path}: col 的参数必须是列名字符串")
        if body not in schema:
            raise CompileError(f"{path}: 未知列 {body!r}")
        node.col_name = body
    elif op == "literal":
        if body is None:
            node.lit_kind, node.literal = "null", None
        elif isinstance(body, bool):
            node.lit_kind, node.literal = "bool", body
        elif isinstance(body, int):
            if not (INT64_MIN <= body <= INT64_MAX):
                raise CompileError(f"{path}: int 字面量超出 int64 范围: {body}")
            node.lit_kind, node.literal = "int", body
        elif isinstance(body, str):
            node.lit_kind, node.literal = "text", body
        else:
            raise CompileError(f"{path}: 不支持的 literal（仅 int64/text/bool/null）: {body!r}")
    elif op in _BINARY:
        if not isinstance(body, list) or len(body) != 2:
            raise CompileError(f"{path}: {op} 需要恰好 2 个子节点列表")
        node.children = [_parse(body[i], f"{path}/{i}", schema) for i in range(2)]
    elif op in _UNARY:
        if not isinstance(body, dict):
            raise CompileError(f"{path}: {op} 需要单个子节点对象")
        node.children = [_parse(body, f"{path}/0", schema)]
    elif op == "if":
        if not isinstance(body, list) or len(body) != 3:
            raise CompileError(f"{path}: if 需要 [cond, then, else] 三个子节点")
        node.children = [_parse(body[i], f"{path}/{i}", schema) for i in range(3)]
    elif op == "coalesce":
        if not isinstance(body, list) or len(body) < 1:
            raise CompileError(f"{path}: coalesce 需要至少 1 个子节点列表")
        node.children = [_parse(b, f"{path}/{i}", schema) for i, b in enumerate(body)]
    return node


# ---------------------------------------------------------------- 类型定型

def _require(child: Node, t: str, parent: Node) -> None:
    """要求子节点类型为 t；NULL 字面量（type 为 None）按上下文定型为 t。"""
    if child.type is None:
        child.type = t
    elif child.type != t:
        raise CompileError(
            f"{parent.path}: {parent.op} 要求 {t}，子节点 {child.path} 是 {child.type}"
        )


def _unify(parent: Node, children: list[Node]) -> str:
    """统一若干子节点类型；全为 NULL 字面量则无法定型，预检拒绝。"""
    known = {c.type for c in children if c.type is not None}
    if len(known) > 1:
        raise CompileError(f"{parent.path}: {parent.op} 分支类型不一致: {sorted(known)}")
    if not known:
        raise CompileError(f"{parent.path}: NULL 字面量无法从上下文定型")
    t = known.pop()
    for c in children:
        if c.type is None:
            c.type = t
    return t


def _infer(node: Node, schema) -> None:
    op = node.op
    if op == "col":
        node.type, node.nullable = schema[node.col_name]
    elif op == "literal":
        node.type = {"int": TYPE_INT, "text": TYPE_TEXT, "bool": TYPE_BOOL,
                     "null": None}[node.lit_kind]
        node.nullable = node.lit_kind == "null"
    elif op in ("add", "div"):
        for c in node.children:
            _infer(c, schema)
            _require(c, TYPE_INT, node)
        node.type = TYPE_INT
        node.nullable = any(c.nullable for c in node.children)
    elif op == "to_int":
        _infer(node.children[0], schema)
        _require(node.children[0], TYPE_TEXT, node)
        node.type = TYPE_INT
        node.nullable = node.children[0].nullable
    elif op in ("eq", "gt"):
        for c in node.children:
            _infer(c, schema)
        t = _unify(node, node.children)
        if op == "gt" and t == TYPE_BOOL:
            raise CompileError(f"{node.path}: gt 不支持 bool")
        node.type = TYPE_BOOL
        node.nullable = any(c.nullable for c in node.children)
    elif op == "is_null":
        _infer(node.children[0], schema)
        node.type = TYPE_BOOL
        node.nullable = False
    elif op == "not":
        _infer(node.children[0], schema)
        _require(node.children[0], TYPE_BOOL, node)
        node.type = TYPE_BOOL
        node.nullable = node.children[0].nullable
    elif op == "if":
        _infer(node.children[0], schema)
        _require(node.children[0], TYPE_BOOL, node)
        _infer(node.children[1], schema)
        _infer(node.children[2], schema)
        node.type = _unify(node, node.children[1:])
        node.nullable = node.children[1].nullable or node.children[2].nullable
    elif op == "coalesce":
        for c in node.children:
            _infer(c, schema)
        node.type = _unify(node, node.children)
        node.nullable = all(c.nullable for c in node.children)
    elif op in ("and", "or"):
        for c in node.children:
            _infer(c, schema)
            _require(c, TYPE_BOOL, node)
        node.type = TYPE_BOOL
        node.nullable = any(c.nullable for c in node.children)


def _check_all_typed(node: Node) -> None:
    if node.type is None:
        raise CompileError(f"{node.path}: NULL 字面量无法从上下文定型")
    for c in node.children:
        _check_all_typed(c)
