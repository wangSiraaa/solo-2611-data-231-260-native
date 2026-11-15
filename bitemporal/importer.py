"""revisions.jsonl 导入入口：校验、物化、导出 history.jsonl。

校验在任何状态变更之前完成（逐行解析与整文件规则校验先跑一遍），
随后在单个事务中物化；任一步失败则回滚，已建库不保留半成品数据。
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Iterator, Optional

from .model import Revision, is_finite_scalar
from .store import HistoryStore

_KINDS = ("assert", "retract")


class RevisionError(ValueError):
    """修订数据不合法（在本次状态变更之前抛出）。"""


def _parse_line(raw: dict[str, Any], line_no: int) -> Revision:
    """解析并校验单行 JSON，非法即抛 RevisionError，不触碰任何状态。"""

    def fail(msg: str) -> None:
        raise RevisionError(f"第 {line_no} 行修订非法：{msg}")

    if not isinstance(raw, dict):
        fail("修订必须是 JSON 对象")

    for field in ("id", "entity", "known_at", "from", "to", "kind"):
        if field not in raw:
            fail(f"缺少字段 {field!r}")

    rev_id = raw["id"]
    if not isinstance(rev_id, (str, int, bool)) or rev_id is None:
        fail("id 必须是字符串或整数标量")
    if isinstance(rev_id, bool):
        fail("id 不能是布尔值")

    entity = raw["entity"]
    if not isinstance(entity, str) or not entity:
        fail("entity 必须是非空字符串")

    known_at = raw["known_at"]
    if isinstance(known_at, bool) or not isinstance(known_at, int):
        fail("known_at 必须是整数")

    lo = raw["from"]
    if isinstance(lo, bool) or not isinstance(lo, int):
        fail("from 必须是整数（允许负数）")

    hi = raw["to"]
    if hi is not None and (isinstance(hi, bool) or not isinstance(hi, int)):
        fail("to 必须是整数或 null")
    if hi is not None and not lo < hi:
        fail(f"非法有效期 [from,to)：from={lo}, to={hi}，要求 from < to")

    kind = raw["kind"]
    if kind not in _KINDS:
        fail(f"kind 必须是 assert 或 retract，实际为 {kind!r}")

    if kind == "assert":
        if "value" not in raw:
            fail("assert 修订必须带 value")
        value = raw["value"]
        if not is_finite_scalar(value):
            fail("value 仅限有限 JSON 数值、字符串、布尔或 null")
    else:  # retract 不接受值，明确失败而非静默忽略
        if "value" in raw:
            fail("retract 修订不能携带 value；assert null 请使用 kind=assert")
        value = None

    return Revision(
        rev_id=rev_id,
        entity=entity,
        known_at=known_at,
        lo=lo,
        hi=hi,
        kind=kind,
        value=value,
    )


def parse_revisions(path: str) -> list[Revision]:
    """解析整个文件，完成全部静态/顺序校验，通过后才返回修订列表。"""

    revisions: list[Revision] = []
    seen_ids: set[Any] = set()
    prev_known_at: Optional[int] = None

    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RevisionError(f"第 {line_no} 行不是合法 JSON：{exc}") from exc

            revision = _parse_line(raw, line_no)

            if revision.rev_id in seen_ids:
                raise RevisionError(
                    f"第 {line_no} 行修订 id 重复：{revision.rev_id!r}"
                )
            seen_ids.add(revision.rev_id)

            if prev_known_at is not None and revision.known_at <= prev_known_at:
                raise RevisionError(
                    f"第 {line_no} 行 known_at={revision.known_at} "
                    f"未严格递增（上一时刻为 {prev_known_at}）"
                )
            prev_known_at = revision.known_at

            revisions.append(revision)

    return revisions


def export_history_jsonl(store: HistoryStore, out_path: str) -> int:
    """把物化历史表导出为 history.jsonl，返回导出行数。"""

    directory = os.path.dirname(out_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    rows = store.fetch_all_rows()
    with open(out_path, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            fh.write("\n")
    return len(rows)


def import_revisions(
    revisions_path: str,
    db_path: str = "history/bitemporal.db",
    history_path: str = "history/history.jsonl",
) -> dict[str, Any]:
    """导入入口：全量校验 -> 事务物化 -> 导出 JSONL。

    任何校验或物化失败都会回滚事务并抛出 RevisionError，
    失败前不会改写既有产物（导出只在提交成功后进行）。
    """

    # 第一阶段：全部校验完成，期间不触碰数据库
    revisions = parse_revisions(revisions_path)

    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    if os.path.exists(db_path):
        os.remove(db_path)  # 全量重建，保证可重复导入

    store = HistoryStore(db_path)
    try:
        store.begin()
        try:
            for revision in revisions:
                store.apply_revision(revision)
            store.commit()
        except Exception:
            store.rollback()
            raise
        # 提交成功后才导出独立可查询产物
        exported = export_history_jsonl(store, history_path)
    finally:
        store.close()

    return {
        "revisions": len(revisions),
        "history_rows": exported,
        "entities": sorted({r.entity for r in revisions}),
        "db_path": db_path,
        "history_path": history_path,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="把 revisions.jsonl 物化为双时态历史（SQLite + history.jsonl）"
    )
    parser.add_argument("revisions", help="输入 revisions.jsonl 路径")
    parser.add_argument(
        "--db", default="history/bitemporal.db", help="SQLite 历史库输出路径"
    )
    parser.add_argument(
        "--history", default="history/history.jsonl", help="history.jsonl 输出路径"
    )
    args = parser.parse_args(argv)

    summary = import_revisions(args.revisions, args.db, args.history)
    print(
        "导入完成：{revisions} 条修订，{history_rows} 行历史，"
        "实体 {entities}".format(**summary)
    )
    print(f"SQLite 历史库：{summary['db_path']}")
    print(f"JSONL 历史表：{summary['history_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
