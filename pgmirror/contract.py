"""contract.json 的加载与校验。

contract 声明协议版本、允许的表与主键、支持的类型 OID 以及唯一允许的
关系变更形态。解码出的 Relation 元数据必须与 contract 对得上，否则
拒绝继续，而不是悄悄猜测。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from .errors import UnsupportedFeatureError


@dataclass(frozen=True)
class TableContract:
    namespace: str
    name: str
    primary_key: Tuple[str, ...]

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}.{self.name}"


@dataclass(frozen=True)
class Contract:
    protocol: int
    framing: str
    tables: Tuple[TableContract, ...]
    supported_type_oids: Dict[int, str]
    relation_changes: str

    def lookup_table(self, namespace: str, name: str) -> TableContract:
        for table in self.tables:
            if table.namespace == namespace and table.name == name:
                return table
        raise UnsupportedFeatureError(
            f"relation {namespace}.{name} is not declared in contract.json"
        )


def load_contract(path: str | Path) -> Contract:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if raw.get("protocol") != 1:
        raise UnsupportedFeatureError(
            "only pgoutput protocol version 1 is supported, contract declares %r"
            % raw.get("protocol")
        )
    if raw.get("transaction_streaming") is not False:
        raise UnsupportedFeatureError("streaming transactions are not supported")
    tables = tuple(
        TableContract(
            namespace=t["namespace"],
            name=t["name"],
            primary_key=tuple(t["primary_key"]),
        )
        for t in raw["tables"]
    )
    type_oids = {int(oid): name for oid, name in raw["supported_type_oids"].items()}
    return Contract(
        protocol=raw["protocol"],
        framing=raw["framing"],
        tables=tables,
        supported_type_oids=type_oids,
        relation_changes=raw["relation_changes"],
    )
