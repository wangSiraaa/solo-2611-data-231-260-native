"""事务化镜像引擎：pgoutput 消息流 -> 仅在 Commit 后可见的 SQLite 关系镜像。

设计要点（对应题面硬约束）：

1. 消息只在当前事务的暂存层生效；Commit 时在单个 SQLite 事务里把
   schema 变更与行变更一并落库。任何异常都会丢弃整个暂存事务，
   已提交前缀保持不动，未提交内容绝不泄漏。
2. Relation 消息按“当前定义”解释其后的 tuple；列位置不做永久绑定，
   每次都以最新 Relation 的列名/顺序对齐。允许的 schema 演进只有
   “尾部追加 nullable 且 default NULL 的列”，其余一律 unsupported。
3. tuple 标记 n=真 NULL、u=unchanged TOAST（必须继承该业务行已有值，
   Insert 上非法、无可继承行则失败）、t=长度前缀文本。
4. Update 的 K old tuple 只用于取旧主键定位；非键列上的 n 不代表清空。
   没有 K 时用新 tuple 的键定位，主键变化也不会留下旧行。
5. 不重新执行 source.sql，不连接数据库，不管理复制槽。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contract import Contract, TableContract
from .errors import (
    MalformedMessageError,
    UnknownRelationError,
    UnsupportedFeatureError,
)
from .protocol import (
    BeginMessage,
    ColumnSpec,
    CommitMessage,
    DeleteMessage,
    InsertMessage,
    RelationMessage,
    TupleValue,
    UpdateMessage,
)

# SQLite 存储类型按 contract 支持的 OID 固定映射。
_SQLITE_TYPES = {23: "INTEGER", 25: "TEXT"}
_POSTGRES_TYPE_NAMES = {23: "int4", 25: "text"}


def quote_ident(identifier: str) -> str:
    """SQLite 双引号标识符转义。"""

    return '"' + identifier.replace('"', '""') + '"'


@dataclass
class CommittedRelation:
    """已发布到 SQLite 的关系定义。"""

    relid: int
    namespace: str
    name: str
    replica_identity: str
    columns: List[ColumnSpec]

    @property
    def table_name(self) -> str:
        return self.name

    def column_names(self) -> List[str]:
        return [c.name for c in self.columns]


@dataclass
class StagedOp:
    """事务内一条行变更的暂存记录。"""

    kind: str                       # 'insert' | 'update' | 'delete'
    message_index: int
    frame_offset: int
    relid: int
    key: Dict[str, Any]             # 定位用主键（update 有 K 时为旧主键）
    new_row: Optional[Dict[str, Any]] = None
    full_row_before: Optional[Dict[str, Any]] = None  # 供 records.jsonl 全字段留痕


@dataclass
class _TxnState:
    xid: int
    begin_index: int
    begin_frame_offset: int
    final_lsn: int
    # relid -> 本事务内最新 Relation 定义（提交前不写 SQLite）。
    relations: Dict[int, RelationMessage] = field(default_factory=dict)
    # 表名 -> 本事务内是否需要在提交时建表/加列。
    ops: List[StagedOp] = field(default_factory=list)
    inserts = 0
    updates = 0
    deletes = 0


class Mirror:
    """消费 pgoutput 消息、维护一个从空库开始的 SQLite 关系镜像。"""

    def __init__(self, connection: sqlite3.Connection, contract: Contract) -> None:
        self._db = connection
        self._contract = contract
        # 已提交关系：relid -> CommittedRelation。
        self._committed: Dict[int, CommittedRelation] = {}
        # 表名 -> relid，用于发现 relid 复用等异常情况。
        self._name_to_relid: Dict[str, int] = {}
        self._txn: Optional[_TxnState] = None
        self._init_catalog()

    # ------------------------------------------------------------------ catalog

    def _init_catalog(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS _mirror_relations (
                relid       INTEGER PRIMARY KEY,
                namespace   TEXT NOT NULL,
                name        TEXT NOT NULL,
                replica_identity TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                version     INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS _mirror_transactions (
                xid          INTEGER PRIMARY KEY,
                status       TEXT NOT NULL,
                commit_lsn   INTEGER,
                commit_lsn_hex TEXT,
                end_lsn      INTEGER,
                message_from INTEGER NOT NULL,
                message_to   INTEGER,
                frame_from   INTEGER NOT NULL,
                frame_to     INTEGER,
                inserts      INTEGER NOT NULL,
                updates      INTEGER NOT NULL,
                deletes      INTEGER NOT NULL
            );
            """
        )
        self._db.commit()

    # ------------------------------------------------------------- 消息入口

    def handle_begin(self, msg: BeginMessage, message_index: int, frame_offset: int) -> None:
        if self._txn is not None:
            raise MalformedMessageError(
                f"nested Begin while transaction xid={self._txn.xid} is still open",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        self._txn = _TxnState(
            xid=msg.xid,
            begin_index=message_index,
            begin_frame_offset=frame_offset,
            final_lsn=msg.final_lsn,
        )

    def handle_relation(self, msg: RelationMessage, message_index: int, frame_offset: int) -> None:
        txn = self._require_txn(message_index, frame_offset, "Relation")
        self._validate_relation_against_contract(msg, message_index, frame_offset)
        existing = self._committed.get(msg.relid)
        if existing is None:
            # 全新关系：同事务/历史里不能有别的 relid 占用同名表。
            owner = self._name_to_relid.get(self._qualified(msg))
            if owner is not None and owner != msg.relid:
                raise UnsupportedFeatureError(
                    f"relation name {self._qualified(msg)} already owned by relid={owner}; "
                    "relid reuse/remap is not supported",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )
            txn.relations[msg.relid] = msg
            return
        # 已发布过的关系重发：核心属性必须完全一致，只允许尾部追加 nullable 列。
        self._check_relation_evolution(existing, msg, message_index, frame_offset)
        txn.relations[msg.relid] = msg

    def handle_insert(self, msg: InsertMessage, message_index: int, frame_offset: int) -> StagedOp:
        txn = self._require_txn(message_index, frame_offset, "Insert")
        relation = self._resolve_relation(msg.relid, message_index, frame_offset)
        values = self._decode_tuple(relation, msg.new_tuple, message_index, frame_offset)
        if any(v.marker == "u" for v in msg.new_tuple):
            raise MalformedMessageError(
                "unchanged-TOAST marker 'u' is illegal in an Insert tuple: "
                "there is no prior row value to inherit",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        key = self._project_key(relation, values, message_index, frame_offset)
        if self._row_exists(relation, key):
            raise MalformedMessageError(
                f"insert violates primary key {self._key_names(relation)}={key}; "
                "a row with that key already exists",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        row = {col.name: values[i] for i, col in enumerate(relation.columns)}
        op = StagedOp(
            kind="insert",
            message_index=message_index,
            frame_offset=frame_offset,
            relid=msg.relid,
            key=key,
            new_row=row,
        )
        txn.ops.append(op)
        txn.inserts += 1
        return op

    def handle_update(self, msg: UpdateMessage, message_index: int, frame_offset: int) -> StagedOp:
        txn = self._require_txn(message_index, frame_offset, "Update")
        relation = self._resolve_relation(msg.relid, message_index, frame_offset)
        decoded_new = self._decode_tuple(relation, msg.new_tuple, message_index, frame_offset)

        if msg.old_key is not None:
            old_key = self._decode_key_tuple(relation, msg.old_key, message_index, frame_offset)
        else:
            # 无 K：用新 tuple 自带的主键定位（即使键值没变也是它）。
            old_key = self._project_key(relation, decoded_new, message_index, frame_offset)

        current = self._load_row(relation, old_key)
        if current is None:
            raise MalformedMessageError(
                f"update target with key {self._key_names(relation)}={old_key} does not exist; "
                "no row available for TOAST inheritance",
                message_index=message_index,
                frame_offset=frame_offset,
            )

        # 以当前已有效行（含本事务前序暂存改动）为基底合并。
        merged: Dict[str, Any] = dict(current)
        for i, col in enumerate(relation.columns):
            marker = msg.new_tuple[i].marker
            if marker == "u":
                # unchanged TOAST：继承业务行已有值，绝不能当 NULL 或字面量。
                if col.name not in current or current[col.name] is None:
                    raise MalformedMessageError(
                        f"unchanged-TOAST column {col.name!r} has no existing value to inherit",
                        message_index=message_index,
                        frame_offset=frame_offset,
                    )
                continue
            if marker == "n":
                merged[col.name] = None
            else:
                merged[col.name] = decoded_new[i]

        new_key = self._project_key(relation, merged, message_index, frame_offset)
        # 主键改了就不能与别的现存行撞键（自己的旧键除外）。
        if new_key != old_key and self._row_exists(relation, new_key):
            raise MalformedMessageError(
                f"updated primary key {new_key} already exists on another row",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        op = StagedOp(
            kind="update",
            message_index=message_index,
            frame_offset=frame_offset,
            relid=msg.relid,
            key=dict(old_key),
            new_row=merged,
            full_row_before=dict(current),
        )
        txn.ops.append(op)
        txn.updates += 1
        return op

    def handle_delete(self, msg: DeleteMessage, message_index: int, frame_offset: int) -> StagedOp:
        txn = self._require_txn(message_index, frame_offset, "Delete")
        relation = self._resolve_relation(msg.relid, message_index, frame_offset)
        key = self._decode_key_tuple(relation, msg.key_tuple, message_index, frame_offset)
        current = self._load_row(relation, key)
        if current is None:
            raise MalformedMessageError(
                f"delete target with key {self._key_names(relation)}={key} does not exist",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        op = StagedOp(
            kind="delete",
            message_index=message_index,
            frame_offset=frame_offset,
            relid=msg.relid,
            key=key,
            full_row_before=dict(current),
        )
        txn.ops.append(op)
        txn.deletes += 1
        return op

    def handle_commit(
        self, msg: CommitMessage, message_index: int, frame_offset: int
    ) -> Dict[str, Any]:
        txn = self._require_txn(message_index, frame_offset, "Commit")
        if msg.commit_lsn != txn.final_lsn:
            raise MalformedMessageError(
                f"Commit LSN {msg.commit_lsn:#x} does not match Begin final LSN "
                f"{txn.final_lsn:#x} for xid={txn.xid}",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        record = self._publish(txn, msg, message_index, frame_offset)
        self._txn = None
        return record

    def abort_open_transaction(
        self, cause: Exception, message_index: int
    ) -> Optional[Dict[str, Any]]:
        """事务出错时调用：丢弃全部暂存内容，返回写入 transactions.jsonl 的 incomplete/aborted 记录。"""

        if self._txn is None:
            return None
        txn = self._txn
        self._txn = None
        return {
            "xid": txn.xid,
            "status": "aborted",
            "commit_lsn": None,
            "commit_lsn_hex": None,
            "message_from": txn.begin_index,
            "message_to": message_index,
            "frame_from": txn.begin_frame_offset,
            "frame_to": None,
            "inserts": 0,
            "updates": 0,
            "deletes": 0,
            "error": str(cause),
        }

    def mark_incomplete_at_eof(self) -> Optional[Dict[str, Any]]:
        """EOF 处仍有未提交事务：标 incomplete，不发布其中任何记录。"""

        if self._txn is None:
            return None
        txn = self._txn
        self._txn = None
        return {
            "xid": txn.xid,
            "status": "incomplete",
            "commit_lsn": None,
            "commit_lsn_hex": None,
            "message_from": txn.begin_index,
            "message_to": None,
            "frame_from": txn.begin_frame_offset,
            "frame_to": None,
            "inserts": 0,
            "updates": 0,
            "deletes": 0,
            "error": "input ended before Commit message was received",
        }

    @property
    def open_transaction_xid(self) -> Optional[int]:
        return self._txn.xid if self._txn else None

    # ------------------------------------------------------------- 提交/落库

    def _publish(
        self,
        txn: _TxnState,
        commit: CommitMessage,
        commit_index: int,
        commit_frame_offset: int,
    ) -> Dict[str, Any]:
        # 整个提交在一个 SQLite 事务里：任何一步失败整体回滚，镜像看不到半成品。
        try:
            self._db.execute("BEGIN")
            ddl_events = self._apply_schema_ddl(txn)
            for op in txn.ops:
                self._apply_row_op(op, txn.relations.get(op.relid))
            self._write_catalog(txn, commit, commit_index, commit_frame_offset)
            self._db.commit()
        except Exception:
            self._db.rollback()
            raise

        # SQLite 提交成功后才更新内存里的已提交关系。
        for relid, relation in txn.relations.items():
            committed = self._committed.get(relid)
            if committed is None:
                committed = CommittedRelation(
                    relid=relation.relid,
                    namespace=relation.namespace,
                    name=relation.name,
                    replica_identity=relation.replica_identity,
                    columns=list(relation.columns),
                )
                self._committed[relid] = committed
                self._name_to_relid[committed.name] = relid
            else:
                committed.columns = list(relation.columns)

        record = {
            "xid": txn.xid,
            "status": "committed",
            "commit_lsn": commit.commit_lsn,
            "commit_lsn_hex": hex(commit.commit_lsn),
            "end_lsn": commit.end_lsn,
            "end_lsn_hex": hex(commit.end_lsn),
            "commit_timestamp": commit.commit_timestamp,
            "message_from": txn.begin_index,
            "message_to": commit_index,
            "frame_from": txn.begin_frame_offset,
            "frame_to": commit_frame_offset,
            "inserts": txn.inserts,
            "updates": txn.updates,
            "deletes": txn.deletes,
            "relation_versions": ddl_events,
            "operations": [self._op_json(op) for op in txn.ops],
        }
        return record

    def _apply_schema_ddl(self, txn: _TxnState) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for relid, relation in txn.relations.items():
            committed = self._committed.get(relid)
            table = quote_ident(relation.name)
            if committed is None:
                column_defs = []
                for col in relation.columns:
                    sqlite_type = _SQLITE_TYPES[col.type_oid]
                    column_defs.append(f"{quote_ident(col.name)} {sqlite_type}")
                pk_cols = [c.name for c in relation.columns if c.is_key]
                if pk_cols:
                    column_defs.append(
                        "PRIMARY KEY (" + ", ".join(quote_ident(c) for c in pk_cols) + ")"
                    )
                self._db.execute(
                    f"CREATE TABLE {table} ({', '.join(column_defs)})"
                )
                events.append(
                    {
                        "relid": relid,
                        "table": relation.name,
                        "action": "create",
                        "columns": [c.name for c in relation.columns],
                    }
                )
            elif len(relation.columns) > len(committed.columns):
                # 演进合法性已在 handle_relation 校验过，这里只执行追加列。
                for col in relation.columns[len(committed.columns):]:
                    sqlite_type = _SQLITE_TYPES[col.type_oid]
                    # 显式 DEFAULT NULL：旧行新列即为 NULL。
                    self._db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {quote_ident(col.name)} "
                        f"{sqlite_type} DEFAULT NULL"
                    )
                events.append(
                    {
                        "relid": relid,
                        "table": relation.name,
                        "action": "append_columns",
                        "added_columns": [
                            c.name for c in relation.columns[len(committed.columns):]
                        ],
                    }
                )
        return events

    def _apply_row_op(self, op: StagedOp, active_relation: Optional[RelationMessage]) -> None:
        # 提交时以本事务内最新 Relation 为准（新建表或刚追加过列都走这里）。
        column_names = [c.name for c in active_relation.columns]
        table = quote_ident(active_relation.name)
        if op.kind == "insert":
            cols = column_names
            placeholders = ", ".join("?" for _ in cols)
            # 追加列之前暂存的 insert 没有新列键，按 default NULL 补齐。
            self._db.execute(
                f"INSERT INTO {table} ({', '.join(quote_ident(c) for c in cols)}) "
                f"VALUES ({placeholders})",
                [op.new_row.get(c) for c in cols],
            )
        elif op.kind == "update":
            cols = column_names
            assignments = ", ".join(f"{quote_ident(c)} = ?" for c in cols)
            key_names = [c.name for c in active_relation.columns if c.is_key]
            where = " AND ".join(f"{quote_ident(name)} IS ?" for name in key_names)
            self._db.execute(
                f"UPDATE {table} SET {assignments} WHERE {where}",
                [op.new_row.get(c) for c in cols] + [op.key[name] for name in key_names],
            )
        elif op.kind == "delete":
            key_names = [c.name for c in active_relation.columns if c.is_key]
            where = " AND ".join(f"{quote_ident(name)} IS ?" for name in key_names)
            self._db.execute(
                f"DELETE FROM {table} WHERE {where}",
                [op.key[name] for name in key_names],
            )

    def _write_catalog(
        self,
        txn: _TxnState,
        commit: CommitMessage,
        commit_index: int,
        commit_frame_offset: int,
    ) -> None:
        for relid, relation in txn.relations.items():
            prior = self._db.execute(
                "SELECT version FROM _mirror_relations WHERE relid = ?", (relid,)
            ).fetchone()
            columns_json = json.dumps(
                [
                    {
                        "flags": c.flags,
                        "name": c.name,
                        "type_oid": c.type_oid,
                        "type": _POSTGRES_TYPE_NAMES[c.type_oid],
                        "typmod": c.typmod,
                    }
                    for c in relation.columns
                ],
                ensure_ascii=False,
            )
            if prior is None:
                self._db.execute(
                    "INSERT INTO _mirror_relations(relid, namespace, name, replica_identity, "
                    "columns_json, version) VALUES (?, ?, ?, ?, ?, 1)",
                    (relid, relation.namespace, relation.name, relation.replica_identity, columns_json),
                )
            else:
                self._db.execute(
                    "UPDATE _mirror_relations SET replica_identity = ?, columns_json = ?, "
                    "version = ? WHERE relid = ?",
                    (
                        relation.replica_identity,
                        columns_json,
                        prior[0] + 1,
                        relid,
                    ),
                )
        self._db.execute(
            "INSERT INTO _mirror_transactions(xid, status, commit_lsn, commit_lsn_hex, end_lsn, "
            "message_from, message_to, frame_from, frame_to, inserts, updates, deletes) "
            "VALUES (?, 'committed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                txn.xid,
                commit.commit_lsn,
                hex(commit.commit_lsn),
                commit.end_lsn,
                txn.begin_index,
                commit_index,
                txn.begin_frame_offset,
                commit_frame_offset,
                txn.inserts,
                txn.updates,
                txn.deletes,
            ),
        )

    # ------------------------------------------------------------- 校验/解码

    def _validate_relation_against_contract(
        self,
        msg: RelationMessage,
        message_index: int,
        frame_offset: int,
    ) -> None:
        table_contract: TableContract = self._contract.lookup_table(msg.namespace, msg.name)
        # 主键列必须与 contract 声明一致（以 R 的 key flag 为准并交叉核对）。
        key_from_flags = tuple(c.name for c in msg.columns if c.is_key)
        if key_from_flags != table_contract.primary_key:
            raise UnsupportedFeatureError(
                f"relation {self._qualified(msg)} key columns {key_from_flags} do not match "
                f"contract primary key {table_contract.primary_key}",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        for col in msg.columns:
            if col.type_oid not in self._contract.supported_type_oids:
                raise UnsupportedFeatureError(
                    f"column {col.name!r} uses unsupported type OID {col.type_oid}; "
                    f"supported OIDs are {sorted(self._contract.supported_type_oids)}",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )
            if col.typmod != -1:
                raise UnsupportedFeatureError(
                    f"column {col.name!r} carries typmod {col.typmod}; only typmod -1 is supported",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )
        if len(set(c.name for c in msg.columns)) != len(msg.columns):
            raise MalformedMessageError(
                "duplicate column names in Relation message",
                message_index=message_index,
                frame_offset=frame_offset,
            )

    def _check_relation_evolution(
        self,
        existing: CommittedRelation,
        msg: RelationMessage,
        message_index: int,
        frame_offset: int,
    ) -> None:
        if msg.namespace != existing.namespace or msg.name != existing.name:
            raise UnsupportedFeatureError(
                f"relid={msg.relid} identity changed from "
                f"{existing.namespace}.{existing.name} to {self._qualified(msg)}; "
                "only appending nullable columns is supported",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        if msg.replica_identity != existing.replica_identity:
            raise UnsupportedFeatureError(
                "replica identity changes are not supported",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        old_cols = existing.columns
        new_cols = list(msg.columns)
        if len(new_cols) < len(old_cols):
            raise UnsupportedFeatureError(
                "dropping columns is not supported; only nullable columns may be appended",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        # 前缀列必须逐字段一致：不允许改名字、改类型、改顺序、改 key flag。
        for pos, (old, new) in enumerate(zip(old_cols, new_cols)):
            if (
                old.name != new.name
                or old.type_oid != new.type_oid
                or old.typmod != new.typmod
                or old.flags != new.flags
            ):
                raise UnsupportedFeatureError(
                    f"incompatible schema change at column position {pos} "
                    f"({old.name!r} -> {new.name!r}); only appending nullable columns is supported",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )
        # 追加列必须可空（无 key flag）且类型受支持；default NULL 是本工具的固定语义。
        for new in new_cols[len(old_cols):]:
            if new.is_key:
                raise UnsupportedFeatureError(
                    f"appended column {new.name!r} cannot be part of the primary key",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )

    def _decode_tuple(
        self,
        relation: RelationMessage,
        values: Tuple[TupleValue, ...],
        message_index: int,
        frame_offset: int,
    ) -> List[Any]:
        if len(values) != len(relation.columns):
            raise MalformedMessageError(
                f"tuple has {len(values)} columns but Relation {relation.name!r} defines "
                f"{len(relation.columns)}",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        decoded: List[Any] = []
        for col, value in zip(relation.columns, values):
            if value.marker in ("n", "u"):
                decoded.append(None)  # u 的继承在调用方处理，这里先占位。
                continue
            decoded.append(self._decode_text_value(col, value.raw, message_index, frame_offset))
        return decoded

    def _decode_text_value(
        self,
        col: ColumnSpec,
        raw: bytes,
        message_index: int,
        frame_offset: int,
    ) -> Any:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedMessageError(
                f"column {col.name!r} text value is not valid UTF-8: {exc}",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        if col.type_oid == 25:  # text
            return text
        if col.type_oid == 23:  # int4
            try:
                number = int(text)
            except ValueError:
                raise MalformedMessageError(
                    f"column {col.name!r} expected int4 text but got {text!r}",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )
            if not (-(2 ** 31) <= number <= 2 ** 31 - 1):
                raise MalformedMessageError(
                    f"column {col.name!r} value {number} is out of int4 range",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )
            return number
        raise UnsupportedFeatureError(
            f"unsupported type OID {col.type_oid} for column {col.name!r}",
            message_index=message_index,
            frame_offset=frame_offset,
        )

    def _decode_key_tuple(
        self,
        relation: RelationMessage,
        values: Tuple[TupleValue, ...],
        message_index: int,
        frame_offset: int,
    ) -> Dict[str, Any]:
        """解释 K old tuple。

        默认 replica identity 下 K tuple 与 Relation 等宽：主键列带值，
        非键列通常是 n。非键列上的 n 绝不代表清空业务字段，这里直接忽略。
        """

        if len(values) != len(relation.columns):
            raise MalformedMessageError(
                f"key tuple has {len(values)} columns but Relation defines "
                f"{len(relation.columns)}",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        key: Dict[str, Any] = {}
        for col, value in zip(relation.columns, values):
            if not col.is_key:
                continue  # 非键列（含 n/u）不参与定位，也不产生任何副作用。
            if value.marker != "t":
                raise MalformedMessageError(
                    f"key column {col.name!r} in K tuple must carry a text value, "
                    f"got marker {value.marker!r}",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )
            key[col.name] = self._decode_text_value(
                col, value.raw, message_index, frame_offset
            )
        if not key:
            raise MalformedMessageError(
                "K tuple contains no key columns",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        return key

    # ------------------------------------------------------------- 行可见性

    def _resolve_relation(
        self, relid: int, message_index: int, frame_offset: int
    ) -> RelationMessage:
        if self._txn is not None and relid in self._txn.relations:
            return self._txn.relations[relid]
        committed = self._committed.get(relid)
        if committed is None:
            raise UnknownRelationError(
                relid=relid, message_index=message_index, frame_offset=frame_offset
            )
        # 用已提交定义构造一个等价 RelationMessage 供后续解释。
        return RelationMessage(
            relid=committed.relid,
            namespace=committed.namespace,
            name=committed.name,
            replica_identity=committed.replica_identity,
            columns=tuple(committed.columns),
        )

    def _project_key(
        self,
        relation: RelationMessage,
        decoded_values: Any,
        message_index: int,
        frame_offset: int,
    ) -> Dict[str, Any]:
        # decoded_values 既可能是“按列位置”的列表，也可能是“按列名”的行字典。
        key: Dict[str, Any] = {}
        for i, col in enumerate(relation.columns):
            if not col.is_key:
                continue
            value = decoded_values[i] if isinstance(decoded_values, list) else decoded_values[col.name]
            if value is None:
                raise MalformedMessageError(
                    f"primary key column {col.name!r} cannot be NULL",
                    message_index=message_index,
                    frame_offset=frame_offset,
                )
            key[col.name] = value
        return key

    def _key_names(self, relation: RelationMessage) -> Tuple[str, ...]:
        return tuple(c.name for c in relation.columns if c.is_key)

    def _row_exists(self, relation: RelationMessage, key: Dict[str, Any]) -> bool:
        return self._load_row(relation, key) is not None

    def _load_row(
        self, relation: RelationMessage, key: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """取业务行当前有效值：先看本事务暂存改动，再查已提交 SQLite。"""

        # 1) 本事务内后写覆盖前写；反向扫描让最近一次对该键的影响先生效。
        def _shape(row: Dict[str, Any]) -> Dict[str, Any]:
            # 暂存行可能产生于追加列之前，这里按当前关系补齐新列 NULL。
            return {col.name: row.get(col.name) for col in relation.columns}

        for op in reversed(self._txn.ops):
            if op.relid != relation.relid:
                continue
            if op.kind == "delete":
                if self._same_key(op.key, key):
                    return None
                continue
            if op.kind == "insert":
                if self._same_key(op.key, key):
                    return _shape(op.new_row)
                continue
            # update：op.key 是旧键，new_row 携带可能已变化的新键。
            new_key = {name: op.new_row[name] for name in key}
            if self._same_key(new_key, key) or self._same_key(op.key, key):
                return _shape(op.new_row)
        # 2) 已提交镜像（按当前关系列名读取，旧行缺失的新列自然为 NULL）。
        committed = self._committed.get(relation.relid)
        if committed is None:
            return None
        table = quote_ident(committed.table_name)
        names = [c.name for c in committed.columns if c.is_key]
        where = " AND ".join(f"{quote_ident(name)} IS ?" for name in names)
        params = [key[name] for name in names]
        col_list = ", ".join(quote_ident(c.name) for c in committed.columns)
        cursor = self._db.execute(
            f"SELECT {col_list} FROM {table} WHERE {where}", params
        )
        row = cursor.fetchone()
        if row is None:
            return None
        loaded = {name: row[i] for i, name in enumerate(committed.column_names())}
        # 当前关系可能刚在本事务追加过列：旧行没有的新列补 NULL，
        # 保证返回行始终按“当前 Relation”的列集合成形。
        for col in relation.columns:
            loaded.setdefault(col.name, None)
        return loaded

    @staticmethod
    def _same_key(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        return a == b

    # ------------------------------------------------------------- 工具

    def _require_txn(self, message_index: int, frame_offset: int, what: str) -> _TxnState:
        if self._txn is None:
            raise MalformedMessageError(
                f"{what} message outside of an open transaction (no Begin seen)",
                message_index=message_index,
                frame_offset=frame_offset,
            )
        return self._txn

    @staticmethod
    def _qualified(msg: RelationMessage) -> str:
        return f"{msg.namespace}.{msg.name}"

    def _op_json(self, op: StagedOp) -> Dict[str, Any]:
        payload = {
            "op": op.kind,
            "message_index": op.message_index,
            "frame_offset": op.frame_offset,
            "relid": op.relid,
            "key": op.key,
        }
        if op.new_row is not None:
            payload["new_row"] = op.new_row
        if op.full_row_before is not None:
            payload["row_before"] = op.full_row_before
        return payload
