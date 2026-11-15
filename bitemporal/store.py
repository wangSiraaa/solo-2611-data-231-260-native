"""双时态历史的 SQLite 物化与点查询。

历史表 ``history`` 的每一行是一个双时态段：

* ``valid_from`` / ``valid_to``：有效时间半开区间，``valid_to IS NULL`` 为正无穷；
* ``sys_from`` / ``sys_to``：系统时间（知识时刻）半开区间，
  ``sys_to IS NULL`` 表示该行当前仍开放；
* ``value_json``：事实值的 JSON 文本（``"null"`` 表示存在且值为 JSON null，
  与 SQL NULL 无关；撤回不产生任何行）。

查询只从已物化的历史表按系统时间 + 有效时间过滤，不重放修订事件。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional

from .model import Revision, Segment, apply_revision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    entity     TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_to   INTEGER,
    sys_from   INTEGER NOT NULL,
    sys_to     INTEGER,
    value_json TEXT NOT NULL
);
"""

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_history_entity_sys "
    "ON history(entity, sys_from, sys_to);",
    "CREATE INDEX IF NOT EXISTS idx_history_entity_valid "
    "ON history(entity, valid_from, valid_to);",
]


def _encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _decode(text: str) -> Any:
    return json.loads(text)


class HistoryStore:
    """打开/创建一个双时态历史库，并负责按修订物化历史行。"""

    def __init__(self, path: str = ":memory:"):
        # check_same_thread=False 便于测试；本项目不承诺多进程并发
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(_SCHEMA)
        for stmt in _INDEXES:
            self.conn.execute(stmt)
        # 由导入器显式管理事务
        self.conn.isolation_level = None
        # entity -> 当前规范段列表（内存态，由本 store 的修订累积而成）
        self._current: dict[str, list[Segment]] = {}

    def begin(self) -> None:
        self.conn.execute("BEGIN")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        self.conn.execute("ROLLBACK")

    def close(self) -> None:
        self.conn.close()

    def count_rows(self) -> int:
        cur = self.conn.execute("SELECT COUNT(*) FROM history")
        return int(cur.fetchone()[0])

    def apply_revision(self, revision: Revision) -> None:
        """物化一条修订：差异关闭旧行、新增行从本次 known_at 开放。

        规范化身份规则（边界与值完全相同才视为同一段）：

        * 新旧列表中身份相同的段保留原 ``sys_from``，开放行继续开放；
        * 旧列表中消失或边界变化的段，其开放行在 ``known_at`` 关闭；
        * 新列表中出现的新段，从 ``known_at`` 开始一条新开放行。
        """

        entity = revision.entity
        old_segments = self._current.get(entity, [])
        new_segments = apply_revision(old_segments, revision)

        old_by_key = {seg.key(): seg for seg in old_segments}
        new_by_key = {seg.key(): seg for seg in new_segments}

        # 1) 关闭旧列表中消失或边界变化的段（新列表里没有相同身份）
        for key, seg in old_by_key.items():
            if key not in new_by_key:
                self.conn.execute(
                    "UPDATE history SET sys_to = ? "
                    "WHERE entity = ? AND valid_from = ? "
                    "AND valid_to IS NOT DISTINCT FROM ? "
                    "AND sys_to IS NULL",
                    (
                        revision.known_at,
                        entity,
                        seg.lo,
                        seg.hi,
                    ),
                )

        # 2) 新列表中身份全新的段，插入从 known_at 开放的历史行
        for key, seg in new_by_key.items():
            if key not in old_by_key:
                self.conn.execute(
                    "INSERT INTO history "
                    "(entity, valid_from, valid_to, sys_from, sys_to, value_json) "
                    "VALUES (?, ?, ?, ?, NULL, ?)",
                    (
                        entity,
                        seg.lo,
                        seg.hi,
                        revision.known_at,
                        _encode(seg.value),
                    ),
                )

        self._current[entity] = new_segments

    def fetch_all_rows(self) -> list[dict[str, Any]]:
        """读取全部历史行（用于导出与校验），按双时态坐标排序。"""

        cur = self.conn.execute(
            "SELECT entity, valid_from, valid_to, sys_from, sys_to, value_json "
            "FROM history ORDER BY entity, sys_from, "
            "valid_from, (valid_to IS NULL), valid_to"
        )
        return [_row_to_dict(row) for row in cur.fetchall()]


def _row_to_dict(row: sqlite3.Row | tuple) -> dict[str, Any]:
    entity, valid_from, valid_to, sys_from, sys_to, value_json = row
    return {
        "entity": entity,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "sys_from": sys_from,
        "sys_to": sys_to,
        "value": _decode(value_json),
    }


def query(
    entity: str,
    valid_at: int,
    known_at: int,
    db_path: str = "history/bitemporal.db",
) -> dict[str, Any]:
    """点查询：知识切片 K=known_at 下，valid_at 时刻的事实。

    直接从物化历史表读取，不重放事件。命中条件：

    * ``sys_from <= known_at < sys_to``（``sys_to IS NULL`` 表示仍开放）；
    * ``valid_from <= valid_at < valid_to``（``valid_to IS NULL`` 为正无穷）。

    返回 ``{"found": bool, "value": ..., "row": 历史段dict | None}``。
    """

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT entity, valid_from, valid_to, sys_from, sys_to, value_json "
            "FROM history "
            "WHERE entity = ? "
            "AND sys_from <= ? AND (sys_to IS NULL OR sys_to > ?) "
            "AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)",
            (entity, known_at, known_at, valid_at, valid_at),
        )
        rows = cur.fetchall()
    finally:
        conn.close()

    if len(rows) > 1:
        raise RuntimeError(
            f"历史表异常：{entity} 在 valid_at={valid_at}, "
            f"known_at={known_at} 命中 {len(rows)} 个重叠段"
        )
    if not rows:
        return {"found": False, "value": None, "row": None}
    row = _row_to_dict(rows[0])
    return {"found": True, "value": row["value"], "row": row}


def slice_at(
    entity: str, known_at: int, db_path: str = "history/bitemporal.db"
) -> list[dict[str, Any]]:
    """读取某 entity 在知识切片 K=known_at 可见的全部历史段（按有效时间排序）。"""

    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "SELECT entity, valid_from, valid_to, sys_from, sys_to, value_json "
            "FROM history "
            "WHERE entity = ? "
            "AND sys_from <= ? AND (sys_to IS NULL OR sys_to > ?) "
            "ORDER BY valid_from",
            (entity, known_at, known_at),
        )
        rows = cur.fetchall()
    finally:
        conn.close()
    return [_row_to_dict(row) for row in rows]
