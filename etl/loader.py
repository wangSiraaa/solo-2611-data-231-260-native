"""读取并校验 schema / columns / expressions 三个输入文件。"""
from __future__ import annotations

import json
from pathlib import Path

from .model import BASE_TYPES, INT64_MAX, INT64_MIN, CompileError


def _load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise CompileError(f"无法读取 {path}: {e}") from e


def load_schema(path: Path) -> dict[str, tuple[str, bool]]:
    """返回 {列名: (基础类型, 可空)}。类型串形如 int64 / text? / bool。"""
    raw = _load_json(path)
    if not isinstance(raw, dict) or not raw:
        raise CompileError("schema.json 必须是非空对象")
    schema: dict[str, tuple[str, bool]] = {}
    for name, t in raw.items():
        if not isinstance(name, str) or not isinstance(t, str):
            raise CompileError(f"schema 条目非法: {name!r}: {t!r}")
        nullable = t.endswith("?")
        base = t[:-1] if nullable else t
        if base not in BASE_TYPES:
            raise CompileError(f"schema 列 {name!r} 类型非法: {t!r}")
        schema[name] = (base, nullable)
    return schema


def load_columns(path: Path, schema: dict[str, tuple[str, bool]]) -> tuple[dict[str, list], int]:
    """校验列数据与 schema 一致，返回 ({列名: 值列表}, 行数)。"""
    raw = _load_json(path)
    if not isinstance(raw, dict):
        raise CompileError("columns.json 必须是对象")
    if set(raw) != set(schema):
        raise CompileError(
            f"columns 与 schema 列不一致: 多出 {sorted(set(raw) - set(schema))}, "
            f"缺少 {sorted(set(schema) - set(raw))}"
        )
    n_rows: int | None = None
    for name, values in raw.items():
        if not isinstance(values, list):
            raise CompileError(f"列 {name!r} 必须是数组")
        if n_rows is None:
            n_rows = len(values)
        elif len(values) != n_rows:
            raise CompileError(f"列 {name!r} 行数 {len(values)} 与其他列 {n_rows} 不一致")
        base, nullable = schema[name]
        for i, v in enumerate(values):
            _check_value(name, i, v, base, nullable)
    return raw, (n_rows or 0)


def _check_value(name: str, i: int, v, base: str, nullable: bool) -> None:
    if v is None:
        if not nullable:
            raise CompileError(f"列 {name!r} 第 {i} 行为 NULL，但 schema 不允许")
        return
    if base == "int64":
        # 注意 Python 中 bool 是 int 子类，必须先排除。
        if isinstance(v, bool) or not isinstance(v, int):
            raise CompileError(f"列 {name!r} 第 {i} 行不是 int64: {v!r}")
        if not (INT64_MIN <= v <= INT64_MAX):
            raise CompileError(f"列 {name!r} 第 {i} 行超出 int64 范围: {v!r}")
    elif base == "text":
        if not isinstance(v, str):
            raise CompileError(f"列 {name!r} 第 {i} 行不是 text: {v!r}")
    else:  # bool
        if not isinstance(v, bool):
            raise CompileError(f"列 {name!r} 第 {i} 行不是 bool: {v!r}")


def load_expressions(path: Path) -> list[dict]:
    """返回 [{name, expr}]，仅做外层结构检查；AST 细节由编译器预检。"""
    raw = _load_json(path)
    if not isinstance(raw, list) or not raw:
        raise CompileError("expressions.json 必须是非空数组")
    seen: set[str] = set()
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or set(item) != {"name", "expr"}:
            raise CompileError(f"表达式 #{i} 必须恰含 name 与 expr")
        name = item["name"]
        if not isinstance(name, str) or not name:
            raise CompileError(f"表达式 #{i} 的 name 非法")
        if name in seen:
            raise CompileError(f"表达式名重复: {name!r}")
        seen.add(name)
    return raw
