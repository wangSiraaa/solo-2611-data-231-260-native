"""source 目录：读取 sources.json 声明与 JSONL 实际数据，并核验声明的物理顺序。

source 声明一个全局排序键列表；真实数据必须逐相邻行符合该描述符，
否则视为虚假声明直接失败，规划和执行都不会盲信标签。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import OrderingError, PlanError
from .ordering import normalize_specs, verify_sorted

_TYPES = {"int", "text", "bool", "float"}


def _check_type(value: Any, type_name: str, where: str) -> None:
    nullable = type_name.endswith("?")
    base = type_name[:-1] if nullable else type_name
    if base not in _TYPES:
        raise PlanError(f"{where}: 未知类型 {type_name!r}")
    if value is None:
        if not nullable:
            raise OrderingError(f"{where}: 非空列收到 NULL")
        return
    # bool 是 int 的子类，int 列必须显式拒绝 true/false。
    if base == "int":
        if not isinstance(value, int) or isinstance(value, bool):
            raise OrderingError(f"{where}: 值 {value!r} 不是 int")
    elif base == "float":
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise OrderingError(f"{where}: 值 {value!r} 不是 float")
    elif base == "text":
        if not isinstance(value, str):
            raise OrderingError(f"{where}: 值 {value!r} 不是 text")
    elif base == "bool":
        if not isinstance(value, bool):
            raise OrderingError(f"{where}: 值 {value!r} 不是 bool")


class Source:
    def __init__(self, name: str, declaration: dict):
        self.name = name
        if not isinstance(declaration, dict):
            raise PlanError(f"source {name} 声明必须是对象")
        try:
            relative_path = declaration["path"]
        except KeyError as exc:
            raise PlanError(f"source {name} 缺少字段 {exc.args[0]!r}") from exc
        schema = declaration.get("schema")
        if not isinstance(schema, dict) or not schema:
            raise PlanError(f"source {name} 的 schema 必须是非空对象")
        for field_name, type_name in schema.items():
            if not isinstance(type_name, str):
                raise PlanError(f"source {name} 列 {field_name} 类型非法")
        # 声明中的数据路径相对进程工作目录（与 fixtures/sources.json 的写法一致），
        # 仅当声明为绝对路径时才直接使用。
        declared_path = Path(relative_path)
        self.path = declared_path.resolve() if declared_path.is_absolute() else (
            Path.cwd() / declared_path
        )
        self.schema: dict[str, str] = dict(schema)
        self.order_specs = normalize_specs(declaration.get("order", []))
        for spec in self.order_specs:
            if spec["field"] not in self.schema:
                raise PlanError(
                    f"source {name} 排序键 {spec['field']} 不在 schema 中"
                )

    def load(self) -> list[dict]:
        """读取 JSONL，逐行核验 schema 与声明的全局排序。"""
        if not self.path.exists():
            raise PlanError(f"source {self.name} 数据文件不存在: {self.path}")
        rows: list[dict] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, raw in enumerate(handle, start=1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise PlanError(
                        f"source {self.name} 第 {line_no} 行不是合法 JSON"
                    ) from exc
                if not isinstance(row, dict):
                    raise PlanError(
                        f"source {self.name} 第 {line_no} 行必须是 JSON 对象"
                    )
                where = f"source {self.name} 第 {line_no} 行"
                if set(row.keys()) != set(self.schema.keys()):
                    extra = set(row.keys()) - set(self.schema.keys())
                    missing = set(self.schema.keys()) - set(row.keys())
                    raise OrderingError(
                        f"{where} 列集合不符 (多余 {sorted(extra)}, "
                        f"缺失 {sorted(missing)})"
                    )
                for field_name, type_name in self.schema.items():
                    _check_type(row[field_name], type_name, f"{where}.{field_name}")
                rows.append(row)
        verify_sorted(rows, self.order_specs, f"source {self.name}")
        return rows


class Catalog:
    def __init__(self, sources_path: str | Path):
        sources_path = Path(sources_path).resolve()
        with sources_path.open("r", encoding="utf-8") as handle:
            declarations = json.load(handle)
        if not isinstance(declarations, dict):
            raise PlanError("sources.json 顶层必须是对象")
        self._sources: dict[str, Source] = {}
        self._cache: dict[str, list[dict]] = {}
        for name, declaration in declarations.items():
            self._sources[name] = Source(name, declaration)

    @property
    def names(self) -> list[str]:
        return list(self._sources)

    def source(self, name: str) -> Source:
        if name not in self._sources:
            raise PlanError(f"未知 source: {name!r}")
        return self._sources[name]

    def load(self, name: str) -> list[dict]:
        """带缓存地加载；核验只在真实读取时发生一次。"""
        if name not in self._cache:
            self._cache[name] = self.source(name).load()
        # 返回浅拷贝，调用方对行列表的变动不影响其他使用者；
        # 行 dict 本身只读使用，规划/执行不原地改列。
        return [dict(row) for row in self._cache[name]]
