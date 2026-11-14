"""乱序候选的有界暂存。

内存中保留的**完整未配对记录**数严格不超过 config 的窗口
（pending_record_limit）；窗口满时最早进入的记录整体溢出到 SQLite。
不自行实现数据库，直接使用标准库 sqlite3。
"""

from __future__ import annotations

import sqlite3
from collections import OrderedDict, deque
from collections.abc import Iterable

from .model import ValidRecord


class BoundedStore:
    def __init__(self, window: int, path: str = ":memory:") -> None:
        if window < 1:
            raise ValueError("窗口必须 >= 1")
        self._window = window
        self._mem: OrderedDict[tuple[bytes, int, int, int], ValidRecord] = OrderedDict()
        self._fifo: deque[tuple[bytes, int, int, int]] = deque()
        self._spill_count = 0
        self._db = sqlite3.connect(path)
        # 允许复用同一文件（重复运行）：表已存在则沿用，随后清空旧数据。
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                read_id      BLOB NOT NULL,
                mate         INTEGER NOT NULL,
                source_index INTEGER NOT NULL,
                record_no    INTEGER NOT NULL,
                header       BLOB NOT NULL,
                seq          BLOB NOT NULL,
                qual         BLOB NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset   INTEGER NOT NULL,
                PRIMARY KEY (read_id, mate, source_index, record_no)
            )
            """
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_id ON records(read_id)")
        self._db.execute("DELETE FROM records")
        self._db.commit()

    @property
    def spill_count(self) -> int:
        return self._spill_count

    @property
    def memory_records(self) -> int:
        """当前驻留内存的完整未配对记录数（用于测试窗口约束）。"""
        return len(self._mem)

    def add(self, rec: ValidRecord) -> None:
        # 键含记录号：同一来源同一 mate 的重复身份也是合法候选，
        # 必须全部暂存到最后统一裁定 ambiguous，不能提前丢弃或覆盖。
        key = (rec.read_id, rec.mate, rec.source_index, rec.record_no)
        self._mem[key] = rec
        self._fifo.append(key)
        while len(self._mem) > self._window:
            old_key = self._fifo.popleft()
            old = self._mem.pop(old_key)
            self._insert_sqlite(old)
            self._spill_count += 1

    def _insert_sqlite(self, rec: ValidRecord) -> None:
        self._db.execute(
            """
            INSERT INTO records
                (read_id, mate, source_index, record_no, header, seq, qual,
                 start_offset, end_offset)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec.read_id,
                rec.mate,
                rec.source_index,
                rec.record_no,
                rec.header,
                rec.seq,
                rec.qual,
                rec.start_offset,
                rec.end_offset,
            ),
        )

    def flush(self) -> None:
        """所有输入检查结束后调用：把内存剩余全部落盘，得到完整候选集。"""
        for key in list(self._mem.keys()):
            self._insert_sqlite(self._mem.pop(key))
        self._fifo.clear()
        self._db.commit()

    def iter_group(self, read_id: bytes) -> Iterable[ValidRecord]:
        """枚举某 read_id 的全部候选（内存 + 落盘），按来源、mate、记录号。"""
        rows = self._db.execute(
            "SELECT record_no, read_id, mate, header, seq, qual, start_offset, "
            "end_offset, source_index FROM records WHERE read_id = ? "
            "ORDER BY source_index, mate, record_no",
            (read_id,),
        ).fetchall()
        for row in rows:
            yield self._from_row(row)
        for key, rec in self._mem.items():
            if key[0] == read_id:
                yield rec

    def iter_all_ids(self) -> Iterable[bytes]:
        """全部出现过的 read_id。"""
        seen: set[bytes] = set()
        for (rid,) in self._db.execute("SELECT DISTINCT read_id FROM records"):
            seen.add(rid)
            yield rid
        for key in self._mem:
            if key[0] not in seen:
                seen.add(key[0])
                yield key[0]

    def count(self) -> int:
        total = self._db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return total + len(self._mem)

    @staticmethod
    def _from_row(row: tuple) -> ValidRecord:
        record_no, read_id, mate, header, seq, qual, start, end, src = row
        return ValidRecord(
            record_no=record_no,
            read_id=read_id,
            mate=mate,
            header=header,
            seq=seq,
            qual=qual,
            start_offset=start,
            end_offset=end,
            source_index=src,
        )

    def close(self) -> None:
        self._db.close()
