"""离线驱动：读文件、分帧、喂给 Mirror，并产出三份交付物。

输出：
    mirror.sqlite       可查询的关系镜像（仅含已提交事务的结果）
    records.jsonl       每条行变更一条 JSON（含全字段新值/旧值）
    transactions.jsonl  每个事务一条 JSON（xid、commit LSN、消息范围、计数）

错误策略：
    * 事务中途的任何协议/支持范围错误 -> 该事务整体丢弃，记一条 aborted，
      已提交前缀保留，进程以非零码退出。
    * EOF 处仍打开的事务 -> 记一条 incomplete，不发布其中任何记录。
    * 分帧截断 -> 带消息序号与字节偏移的 TruncatedFrameError，同样保留前缀。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .contract import Contract, load_contract
from .engine import Mirror, quote_ident
from .errors import PgMirrorError, TruncatedFrameError
from .framing import iter_frames
from .protocol import parse_message


@dataclass
class RunResult:
    transactions: List[Dict[str, Any]] = field(default_factory=list)
    records: List[Dict[str, Any]] = field(default_factory=list)
    fatal: Optional[Dict[str, Any]] = None
    messages_consumed: int = 0
    open_transaction_xid: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.fatal is None


def _dispatch(mirror: Mirror, message_type: str, parsed: Any, index: int, frame_offset: int) -> None:
    handler = {
        "B": mirror.handle_begin,
        "R": mirror.handle_relation,
        "I": mirror.handle_insert,
        "U": mirror.handle_update,
        "D": mirror.handle_delete,
    }.get(message_type)
    if handler is None:
        # 'C' 在 run() 里单独处理；其余类型解析层已拒绝。
        raise PgMirrorError(
            f"no dispatch for message type {message_type!r}",
            message_index=index,
            frame_offset=frame_offset,
        )
    handler(parsed, index, frame_offset)


def _fatal_payload(exc: PgMirrorError, consumed: int) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "error_type": type(exc).__name__,
        "message": str(exc),
        "message_index": getattr(exc, "message_index", None),
        "frame_offset": getattr(exc, "frame_offset", None),
        "byte_offset": getattr(exc, "byte_offset", None),
    }
    if isinstance(exc, TruncatedFrameError):
        payload["declared_length"] = exc.declared_length
        payload["available"] = exc.available
    if payload["message_index"] is None:
        payload["message_index"] = consumed
    return payload


def run(
    data: bytes,
    contract: Contract,
    connection: sqlite3.Connection,
    *,
    stop_after: Optional[int] = None,
) -> RunResult:
    """把字节流灌入镜像。

    stop_after（含端点的消息序号）用于 inspect：处理完该消息即停，
    此时若事务仍打开，镜像里绝不能出现它的任何行。
    """

    connection.row_factory = sqlite3.Row
    mirror = Mirror(connection, contract)
    result = RunResult()

    try:
        for frame in iter_frames(data):
            if stop_after is not None and frame.index > stop_after:
                break
            message_type, parsed = parse_message(frame.payload, frame.index, frame.frame_offset)
            if message_type == "C":
                txn_record = mirror.handle_commit(parsed, frame.index, frame.frame_offset)
                result.transactions.append(_public_txn_record(txn_record))
                for op in txn_record["operations"]:
                    result.records.append(
                        {
                            "xid": txn_record["xid"],
                            "commit_lsn": txn_record["commit_lsn"],
                            "commit_lsn_hex": txn_record["commit_lsn_hex"],
                            **op,
                        }
                    )
            else:
                _dispatch(mirror, message_type, parsed, frame.index, frame.frame_offset)
            result.messages_consumed += 1
    except PgMirrorError as exc:
        aborted = mirror.abort_open_transaction(
            exc, getattr(exc, "message_index", None) or result.messages_consumed
        )
        if aborted is not None:
            result.transactions.append(aborted)
        result.fatal = _fatal_payload(exc, result.messages_consumed)
    else:
        # inspect 在消息边界停住时，事务可能仍打开：记录其 xid 但绝不发布。
        result.open_transaction_xid = mirror.open_transaction_xid
        if stop_after is None:
            incomplete = mirror.mark_incomplete_at_eof()
            if incomplete is not None:
                result.transactions.append(incomplete)

    return result


def _public_txn_record(txn_record: Dict[str, Any]) -> Dict[str, Any]:
    """transactions.jsonl 保留消息范围/计数/schema 版本；行级操作进 records 流。"""

    return {key: value for key, value in txn_record.items() if key != "operations"}


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def build_outputs(input_path: Path, contract_path: Path, out_dir: Path) -> RunResult:
    out_dir.mkdir(parents=True, exist_ok=True)
    data = Path(input_path).read_bytes()
    contract = load_contract(contract_path)

    sqlite_path = out_dir / "mirror.sqlite"
    if sqlite_path.exists():
        sqlite_path.unlink()  # 每次都从空镜像开始。
    connection = sqlite3.connect(sqlite_path)
    try:
        result = run(data, contract, connection)
    finally:
        connection.close()

    _write_jsonl(out_dir / "transactions.jsonl", result.transactions)
    _write_jsonl(out_dir / "records.jsonl", result.records)
    return result


def inspect_at(
    input_path: Path,
    contract_path: Path,
    through_index: int,
    sql: Optional[str] = None,
) -> Dict[str, Any]:
    """在任一消息边界构建临时镜像并检查已提交内容。

    through_index 为含端点的消息序号（0 基）。处理完该消息即停止；
    若它落在 Begin..Commit 之间，事务保持未提交，查询结果即证明
    中间消息不会提前可见。
    """

    data = Path(input_path).read_bytes()
    contract = load_contract(contract_path)
    connection = sqlite3.connect(":memory:")
    try:
        result = run(data, contract, connection, stop_after=through_index)
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE '\\_mirror\\_%' ESCAPE '\\'"
            ).fetchall()
        ]
        snapshot: Dict[str, Any] = {"tables": {}}
        for table in tables:
            cols = [r[1] for r in connection.execute(f"PRAGMA table_info({quote_ident(table)})")]
            rows = connection.execute(
                f"SELECT {', '.join(quote_ident(c) for c in cols)} FROM {quote_ident(table)}"
            ).fetchall()
            snapshot["tables"][table] = [dict(zip(cols, row)) for row in rows]
        committed = [
            dict(r)
            for r in connection.execute(
                "SELECT xid, status, commit_lsn, commit_lsn_hex, message_from, message_to, "
                "inserts, updates, deletes FROM _mirror_transactions ORDER BY xid"
            ).fetchall()
        ]
        query_result = None
        if sql:
            query_result = [dict(r) for r in connection.execute(sql).fetchall()]
        return {
            "through_message_index": through_index,
            "messages_consumed": result.messages_consumed,
            "open_transaction_xid": result.open_transaction_xid,
            "committed_transactions": committed,
            "snapshot": snapshot,
            "query_result": query_result,
            "fatal": result.fatal,
        }
    finally:
        connection.close()
