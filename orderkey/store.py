"""SQLite BLOB 主索引的构建、重开与区间扫描。

表布局（联合索引方案，普通业务键字节与 row_id 物理分离，不会混淆）：

    CREATE TABLE kv (
        key_blob BLOB NOT NULL,   -- encode() 得到的复合业务键
        row_id   TEXT NOT NULL,   -- 键外独立身份（UTF-8 文本）
        payload  TEXT,            -- 原始 JSONL 行（原样留存，可为 NULL）
        PRIMARY KEY (key_blob, row_id)
    ) WITHOUT ROWID;

* SQLite 只做两件事：按 BLOB 的二进制序排序、按下界/上界做 B-Tree 范围取数；
  查询路径上不存在任何业务列排序或函数计算。
* 业务键相同的多条记录由 row_id 决胜，联合主键保证输出按
  (key_blob 字节序, row_id 字节序) 稳定排列。
* meta 表保存格式版本与 schema 摘要；重开后仅凭 key_blob 即可 decode 还原
  业务键（schema 从 meta 读回并校验）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from .codec import compile_prefix, decode, encode, matches, validate_schema

FORMAT_VERSION = 1
META_TABLE = "kv_meta"
DATA_TABLE = "kv"


def schema_digest(schema: list[dict]) -> str:
    """schema 摘要：规范化 JSON 后取 SHA-256，用于重开时的一致性校验。"""
    canonical = json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class ScanResult:
    """一次 scan 的全部输出与可观测指标。"""

    rows: list[dict[str, Any]]          # 命中记录（已解码业务键 + row_id）
    lower_hex: Optional[str]            # 实际下界（hex）；None 表示无下界
    upper_hex: Optional[str]            # 实际上界（hex）；None 表示无穷上界
    sql: str                            # 实际执行的 SQL
    params: list[bytes]                 # 绑定参数
    keys_read: int                      # 区间内实际读取的键数（残余核对前）
    keys_returned: int                  # 残余核对后返回的键数
    explain: list[str]                  # EXPLAIN QUERY PLAN 输出

    def describe_bounds(self) -> str:
        lo = "-inf" if self.lower_hex is None else self.lower_hex
        hi = "+inf" if self.upper_hex is None else self.upper_hex
        return f"[{lo}, {hi})"


class Store:
    """复合键 BLOB 索引；通过 build() 新建、open() 重开。"""

    def __init__(self, path: str, schema: list[dict], conn: sqlite3.Connection):
        self.path = path
        self.schema = schema
        self._conn = conn

    # ------------------------------------------------------------------ #
    # 构建 / 重开
    # ------------------------------------------------------------------ #
    @classmethod
    def build(cls, path: str, schema: list[dict], rows: list[dict]) -> "Store":
        """按 schema 编码全部行并写入新的 SQLite 文件（已存在则覆盖）。"""
        validate_schema(schema)
        if os.path.exists(path):
            os.remove(path)
        conn = sqlite3.connect(path)
        try:
            conn.execute(
                f"CREATE TABLE {DATA_TABLE} ("
                "key_blob BLOB NOT NULL, "
                "row_id TEXT NOT NULL, "
                "payload TEXT, "
                "PRIMARY KEY (key_blob, row_id)) WITHOUT ROWID"
            )
            conn.execute(
                f"CREATE TABLE {META_TABLE} (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.executemany(
                f"INSERT INTO {META_TABLE}(name, value) VALUES (?, ?)",
                [
                    ("format_version", str(FORMAT_VERSION)),
                    ("schema_json", json.dumps(schema, ensure_ascii=False)),
                    ("schema_digest", schema_digest(schema)),
                ],
            )

            loaded: list[tuple[bytes, str, str]] = []
            for row in rows:
                if "row_id" not in row or not isinstance(row["row_id"], str):
                    raise ValueError("每行必须包含字符串类型的 row_id")
                key_values = {col["name"]: row.get(col["name"]) for col in schema}
                blob = encode(schema, key_values)
                loaded.append((blob, row["row_id"], json.dumps(row, ensure_ascii=False)))
            # 让存储侧也呈现确定性顺序（SQLite 自身仍会按 BLOB 排序维护索引）。
            loaded.sort(key=lambda t: (t[0], t[1].encode("utf-8")))
            conn.executemany(
                f"INSERT INTO {DATA_TABLE}(key_blob, row_id, payload) VALUES (?, ?, ?)",
                loaded,
            )
            conn.commit()
        except Exception:
            conn.close()
            raise
        return cls(path, schema, conn)

    @classmethod
    def open(cls, path: str, schema: Optional[list[dict]] = None) -> "Store":
        """重开已有库；可仅从库内 meta 还原 schema，也可传入 schema 做摘要核对。"""
        conn = sqlite3.connect(path)
        meta = dict(
            conn.execute(f"SELECT name, value FROM {META_TABLE}").fetchall()
        )
        if int(meta.get("format_version", "0")) != FORMAT_VERSION:
            conn.close()
            raise ValueError(f"不支持的格式版本: {meta.get('format_version')!r}")
        stored_schema = json.loads(meta["schema_json"])
        validate_schema(stored_schema)
        if schema is not None and schema_digest(schema) != meta["schema_digest"]:
            conn.close()
            raise ValueError("传入 schema 与库内 schema 摘要不一致")
        return cls(path, stored_schema, conn)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # 扫描
    # ------------------------------------------------------------------ #
    def scan(
        self,
        equals_prefix: Optional[list[Any]] = None,
        starts_with: Optional[str] = None,
    ) -> ScanResult:
        """编译前缀 -> BLOB 半开区间 -> SQLite 范围取数 -> 残余核对。

        不会全表扫描后解码筛选：SQL 始终携带明确的 BLOB 下界/上界，走
        WITHOUT ROWID 联合主键的 B-Tree 区间扫描；无穷上界表达为“不写上界”，
        而不是虚构一个最大字符。
        """
        equals_prefix = list(equals_prefix or [])
        lower, upper = compile_prefix(self.schema, equals_prefix, starts_with)

        clauses = ["key_blob >= ?"]
        params: list[bytes] = [lower]
        if upper is not None:
            clauses.append("key_blob < ?")
            params.append(upper)
        where = " AND ".join(clauses)
        # row_id 是联合主键第二列，相同业务键天然按 row_id 字节序稳定输出，
        # 无需 ORDER BY 之外的业务排序；这里的 ORDER BY 仅复述主键物理序。
        sql = (
            f"SELECT key_blob, row_id, payload FROM {DATA_TABLE} "
            f"WHERE {where} ORDER BY key_blob, row_id"
        )

        explain_rows = self._conn.execute(
            "EXPLAIN QUERY PLAN " + sql, params
        ).fetchall()
        explain = [r[3] for r in explain_rows]

        fetched = self._conn.execute(sql, params).fetchall()
        out: list[dict[str, Any]] = []
        for blob, row_id, payload in fetched:
            key = decode(self.schema, blob)
            # 残余核对：区间是精确前缀，正常情况下全部通过；保留这层防御。
            if not matches(self.schema, key, equals_prefix, starts_with):
                continue
            record = {"row_id": row_id, "key": key}
            if payload is not None:
                record["payload"] = json.loads(payload)
            out.append(record)

        return ScanResult(
            rows=out,
            lower_hex=lower.hex(),
            upper_hex=None if upper is None else upper.hex(),
            sql=sql,
            params=params,
            keys_read=len(fetched),
            keys_returned=len(out),
            explain=explain,
        )

    def count(self) -> int:
        return self._conn.execute(f"SELECT COUNT(*) FROM {DATA_TABLE}").fetchone()[0]
