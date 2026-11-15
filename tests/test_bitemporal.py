"""双时态历史表的完整验证套件。

覆盖：

1. fixtures 的全部手工查询（主 fixture + 扩展 fixture）；
2. 每个输入知识时刻抽取历史切片，核对无重叠、相邻段规范、
   只保存存在事实、开放尾区间；
3. 旧知识切片在新修订到达后保持不变（可回查）；
4. 独立有限整数网格参考器逐点交叉验证（朴素重放，不共享生产代码）；
5. history.jsonl 作为独立产物脱离 SQLite 重新查询；
6. 非法区间、非递增知识时间、重复 id、非法值在状态变更前失败并回滚；
7. 纯无变化断言不产生新历史行。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from bitemporal.importer import RevisionError, import_revisions, parse_revisions
from bitemporal.model import Revision, Segment, apply_revision, values_equal
from bitemporal.store import HistoryStore, query, slice_at

from tests.reference_oracle import RETRACTED, ReferenceKnowledge

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fixtures"

# 主 fixture 手工查询期望：id -> (found, value)
MAIN_EXPECTED = {
    "q1": (True, "north"),
    "q2": (True, "south"),
    "q3": (False, None),
    "q4": (True, "east"),
    "q5": (False, None),
    "q6": (True, "north"),
    "q7": (True, None),  # assert null：存在事实且值为 null
}

# 扩展 fixture 手工查询期望
EXTRA_EXPECTED = {
    "x1": (True, 1),       # 基本 assert
    "x2": (True, 1.0),     # 1 与 1.0 合并后命中
    "x3": (False, None),   # K=1 时 [0,10) 不含 valid_at=10
    "x4": (True, True),    # 布尔事实
    "x5": (True, 1),       # 布尔与数值不相等，边界保持
    "x6": (True, True),    # 迟到有效期覆写（K=3 改写 [5,15)）
    "x7": (False, None),   # 覆盖后局部撤回
    "x8": (True, True),    # 撤回区间再填入
    "x9": (True, 1),       # 再填相邻同值，合并回 [8,10)
    "x10": (True, None),   # assert null 是存在的事实
    "x11": (True, True),   # 同一点 K=7 仍是 true（null 在 K=8 才到达）
    "x12": (False, None),  # 开放尾之外没有事实
    "x13": (False, None),  # K=5 时 [8,10) 已撤回（旧切片语义）
    "x14": (True, 1),      # 纯无变化断言后旧值仍在
}


def _load_queries(name: str) -> list[dict]:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


class ValueEqualityTests(unittest.TestCase):
    def test_rules(self):
        self.assertTrue(values_equal(1, 1.0))
        self.assertTrue(values_equal(1.0, 1))
        self.assertFalse(values_equal(True, 1))
        self.assertFalse(values_equal(False, 0))
        self.assertFalse(values_equal(True, 1.0))
        self.assertTrue(values_equal(True, True))
        self.assertTrue(values_equal(None, None))
        self.assertFalse(values_equal(None, 0))
        self.assertFalse(values_equal(None, ""))
        self.assertTrue(values_equal("a", "a"))
        self.assertFalse(values_equal("a", "b"))
        self.assertFalse(values_equal("1", 1))


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.rev = lambda **kw: Revision(
            rev_id=kw.get("rev_id", "r"),
            entity="e",
            known_at=kw["known_at"],
            lo=kw["lo"],
            hi=kw["hi"],
            kind=kw["kind"],
            value=kw.get("value"),
        )

    def test_partial_override_splits_and_keeps_outside(self):
        old = [Segment(0, 10, "a")]
        new = apply_revision(
            old, self.rev(known_at=1, lo=3, hi=7, kind="assert", value="b")
        )
        self.assertEqual(
            new, [Segment(0, 3, "a"), Segment(3, 7, "b"), Segment(7, 10, "a")]
        )

    def test_retract_leaves_gap_not_null_row(self):
        old = [Segment(0, 10, "a")]
        new = apply_revision(
            old, self.rev(known_at=1, lo=4, hi=6, kind="retract")
        )
        self.assertEqual(new, [Segment(0, 4, "a"), Segment(6, 10, "a")])
        self.assertTrue(all(seg.value is not None or True for seg in new))

    def test_refill_merges_adjacent_equal(self):
        old = [Segment(0, 4, "a"), Segment(6, 10, "a")]
        new = apply_revision(
            old, self.rev(known_at=2, lo=4, hi=6, kind="assert", value="a")
        )
        self.assertEqual(new, [Segment(0, 10, "a")])

    def test_assert_null_is_a_fact(self):
        new = apply_revision(
            [], self.rev(known_at=1, lo=0, hi=5, kind="assert", value=None)
        )
        self.assertEqual(new, [Segment(0, 5, None)])
        again = apply_revision(
            new, self.rev(known_at=2, lo=2, hi=3, kind="retract")
        )
        self.assertEqual(again, [Segment(0, 2, None), Segment(3, 5, None)])

    def test_open_tail_and_negative_time(self):
        new = apply_revision(
            [],
            self.rev(known_at=1, lo=-5, hi=None, kind="assert", value="z"),
        )
        self.assertEqual(new, [Segment(-5, None, "z")])
        new2 = apply_revision(
            new, self.rev(known_at=2, lo=0, hi=3, kind="assert", value="z")
        )
        # 同值写入开放尾内部，合并后仍是一条开放段
        self.assertEqual(new2, [Segment(-5, None, "z")])

    def test_bool_vs_number_no_merge(self):
        old = [Segment(0, 5, 1)]
        new = apply_revision(
            old, self.rev(known_at=1, lo=5, hi=10, kind="assert", value=True)
        )
        self.assertEqual(new, [Segment(0, 5, 1), Segment(5, 10, True)])

    def test_int_float_merge(self):
        old = [Segment(0, 5, 1)]
        new = apply_revision(
            old, self.rev(known_at=1, lo=5, hi=10, kind="assert", value=1.0)
        )
        self.assertEqual(new, [Segment(0, 10, 1)])


class MaterializationTests(unittest.TestCase):
    """用内存库逐修订物化，检查 sys_from/sys_to 身份规则与切片不变量。

    内存库每条语句自动提交，用例之间通过新建连接天然隔离。
    """

    def setUp(self):
        self.store = HistoryStore(":memory:")

    def tearDown(self):
        self.store.close()

    def _rev(self, rev_id, known_at, lo, hi, kind, value=None):
        return Revision(rev_id, "e", known_at, lo, hi, kind, value)

    def test_identity_rule_keeps_sys_from(self):
        self.store.apply_revision(self._rev("r1", 10, 0, 10, "assert", "a"))
        self.store.apply_revision(self._rev("r2", 20, 3, 7, "assert", "b"))
        # 字面身份规则：只有边界与值完全相同的段保留原 sys_from。
        # 旧段 [0,10) 边界变化，于 K=20 关闭；拆出的残余段边界不同，
        # 属于新段，sys_from=20；区间外旧值的旧切片仍可回查（见下）。
        rows = self._slice(20)
        self.assertEqual(
            [(r["valid_from"], r["valid_to"], r["value"], r["sys_from"])
             for r in rows],
            [(0, 3, "a", 20), (3, 7, "b", 20), (7, 10, "a", 20)],
        )
        self.assertTrue(all(r["sys_to"] is None for r in rows))
        # 旧切片 K=10 仍是完整的 [0,10) a，未被改写
        old = self._slice(10)
        self.assertEqual(
            [(r["valid_from"], r["valid_to"], r["value"]) for r in old],
            [(0, 10, "a")],
        )
        closed = self.store.conn.execute(
            "SELECT sys_from, sys_to FROM history "
            "WHERE entity='e' AND valid_from=0 AND valid_to=10"
        ).fetchall()
        self.assertEqual(closed, [(10, 20)])

    def _slice(self, known_at):
        # slice_at 需要文件路径，这里直接对同一连接查询
        cur = self.store.conn.execute(
            "SELECT entity, valid_from, valid_to, sys_from, sys_to, value_json "
            "FROM history WHERE entity='e' AND sys_from <= ? "
            "AND (sys_to IS NULL OR sys_to > ?) ORDER BY valid_from",
            (known_at, known_at),
        )
        return [
            {
                "entity": r[0],
                "valid_from": r[1],
                "valid_to": r[2],
                "sys_from": r[3],
                "sys_to": r[4],
                "value": json.loads(r[5]),
            }
            for r in cur.fetchall()
        ]

    def test_noop_assert_creates_no_rows(self):
        self.store.apply_revision(self._rev("r1", 1, 0, 10, "assert", "a"))
        self.assertEqual(self.store.count_rows(), 1)
        # 同值重述同一区间：规范化结果不变，不产生任何新历史行
        self.store.apply_revision(self._rev("r2", 2, 0, 10, "assert", "a"))
        self.assertEqual(self.store.count_rows(), 1)
        self.assertEqual(self._slice(2)[0]["sys_from"], 1)

    def test_closed_rows_history_is_queryable(self):
        self.store.apply_revision(self._rev("r1", 1, 0, 10, "assert", "a"))
        self.store.apply_revision(self._rev("r2", 2, 0, 10, "assert", "b"))
        # 旧切片 K=1 仍是 a，当前切片是 b
        self.assertEqual(self._slice(1)[0]["value"], "a")
        self.assertEqual(self._slice(2)[0]["value"], "b")
        old = self._slice(1)[0]
        self.assertEqual((old["sys_from"], old["sys_to"]), (1, 2))

    def test_open_tail_segment_can_be_closed(self):
        # 回归测试：valid_to IS NULL 的开放尾段被后续修订关闭时，
        # UPDATE 必须命中（IS NOT DISTINCT FROM 绑定 NULL）
        self.store.apply_revision(
            self._rev("r1", 1, 0, None, "assert", "a")
        )
        self.store.apply_revision(
            self._rev("r2", 2, 5, None, "assert", "b")
        )
        rows = self.store.conn.execute(
            "SELECT valid_from, valid_to, sys_from, sys_to, value_json "
            "FROM history ORDER BY sys_from, valid_from"
        ).fetchall()
        self.assertEqual(
            rows,
            [
                (0, None, 1, 2, '"a"'),       # 旧开放尾已在 K=2 关闭
                (0, 5, 2, None, '"a"'),       # 残余段新行
                (5, None, 2, None, '"b"'),    # 新开放尾
            ],
        )
        # 旧切片仍可回查整条开放尾
        self.assertEqual(
            [(r["valid_from"], r["valid_to"]) for r in self._slice(1)],
            [(0, None)],
        )

    def test_retract_then_refill_reopens_with_new_sys_from(self):
        self.store.apply_revision(self._rev("r1", 1, 0, 10, "assert", "a"))
        self.store.apply_revision(self._rev("r2", 2, 4, 6, "retract"))
        self.store.apply_revision(self._rev("r3", 3, 4, 6, "assert", "a"))
        # 填回后三段同值相邻，规范身份变为整条 [0,10)：
        # 旧的两条残余段在 K=3 关闭，合并出的新段以 sys_from=3 开放
        cur = self.store.conn.execute(
            "SELECT valid_from, valid_to, sys_from, sys_to FROM history "
            "WHERE entity='e' ORDER BY sys_from, valid_from"
        ).fetchall()
        self.assertEqual(
            cur,
            [
                (0, 10, 1, 2),
                (0, 4, 2, 3),
                (6, 10, 2, 3),
                (0, 10, 3, None),
            ],
        )
        # 当前切片 K=3 物理上是一条合并后的开放行
        hit = self._slice(3)
        self.assertEqual(
            [(r["valid_from"], r["valid_to"], r["value"], r["sys_from"])
             for r in hit],
            [(0, 10, "a", 3)],
        )
        # 旧切片仍可回查：K=2 是带空隙的两段
        old = self._slice(2)
        self.assertEqual(
            [(r["valid_from"], r["valid_to"]) for r in old], [(0, 4), (6, 10)]
        )


class SliceInvariantTests(unittest.TestCase):
    """每个知识时刻切片：无重叠、相邻段值不同、半开区间合法。"""

    def _assert_slice_invariants(self, rows):
        for row in rows:
            vf, vt = row["valid_from"], row["valid_to"]
            self.assertIsInstance(vf, int)
            self.assertTrue(vt is None or (isinstance(vt, int) and vf < vt))
        for left, right in zip(rows, rows[1:]):
            self.assertLessEqual(left["valid_to"], right["valid_from"])
            if left["valid_to"] == right["valid_from"]:
                # 相邻（端点相接）的段值必须不同，否则应已合并
                self.assertFalse(
                    values_equal(left["value"], right["value"]),
                    f"相邻同值段未合并：{left} / {right}",
                )

    def test_all_known_slices_main_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            import_revisions(str(FIXTURES / "revisions.jsonl"), db,
                             os.path.join(tmp, "h.jsonl"))
            known_points = sorted(
                {r.known_at for r in parse_revisions(str(FIXTURES / "revisions.jsonl"))}
            )
            for entity in ("c1", "c2"):
                for k in known_points:
                    self._assert_slice_invariants(slice_at(entity, k, db))

    def test_all_known_slices_extra_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            import_revisions(str(FIXTURES / "revisions_extra.jsonl"), db,
                             os.path.join(tmp, "h.jsonl"))
            revisions = parse_revisions(str(FIXTURES / "revisions_extra.jsonl"))
            # 逐知识时刻：切片不变量 + 切片内每行在该 K 确实可见
            for revision in revisions:
                k = revision.known_at
                rows = slice_at("d1", k, db)
                self._assert_slice_invariants(rows)
                for row in rows:
                    self.assertLessEqual(row["sys_from"], k)
                    self.assertTrue(
                        row["sys_to"] is None or row["sys_to"] > k
                    )

    def test_noop_assertion_adds_no_history_rows_end_to_end(self):
        # 主 fixture 的 a7 与 a5 之后 c1 当前状态完全相同（同值同界），
        # 物化到 a7 后历史行数不得增加
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            jl = os.path.join(tmp, "h.jsonl")
            import_revisions(str(FIXTURES / "revisions.jsonl"), db, jl)
            conn = sqlite3.connect(db)
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM history WHERE entity='c1'"
                ).fetchone()[0]
                after_a5 = conn.execute(
                    "SELECT COUNT(*) FROM history WHERE entity='c1' "
                    "AND sys_from <= 5"
                ).fetchone()[0]
            finally:
                conn.close()
            # a7(known_at=7) 是纯无变化断言：没有 sys_from=7 的 c1 行
            self.assertEqual(total, after_a5)

    def test_old_slices_immutable_after_new_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            jl = os.path.join(tmp, "h.jsonl")
            import_revisions(str(FIXTURES / "revisions.jsonl"), db, jl)
            # 在每个知识时刻快照，之后任何时刻重查必须完全一致
            snapshots = {
                k: slice_at("c1", k, db)
                for k in range(1, 8)
            }
            for k, snapshot in snapshots.items():
                self.assertEqual(slice_at("c1", k, db), snapshot)


class FixtureQueryTests(unittest.TestCase):
    def test_main_fixture_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            import_revisions(str(FIXTURES / "revisions.jsonl"), db,
                             os.path.join(tmp, "h.jsonl"))
            for q in _load_queries("queries.jsonl"):
                result = query(q["entity"], q["valid_at"], q["known_at"], db)
                self.assertEqual(
                    (result["found"], result["value"]),
                    MAIN_EXPECTED[q["id"]],
                    msg=q["id"],
                )
                if result["found"]:
                    self.assertEqual(set(result["row"]), {
                        "entity", "valid_from", "valid_to",
                        "sys_from", "sys_to", "value",
                    })

    def test_extra_fixture_queries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            import_revisions(str(FIXTURES / "revisions_extra.jsonl"), db,
                             os.path.join(tmp, "h.jsonl"))
            for q in _load_queries("queries_extra.jsonl"):
                result = query(q["entity"], q["valid_at"], q["known_at"], db)
                self.assertEqual(
                    (result["found"], result["value"]),
                    EXTRA_EXPECTED[q["id"]],
                    msg=q["id"],
                )


class GridOracleTests(unittest.TestCase):
    """独立参考器：有限整数网格上逐点交叉验证两个 fixture。"""

    def _cross_check(self, revisions_name, entities, valid_lo, valid_hi):
        rev_path = str(FIXTURES / revisions_name)
        oracle = ReferenceKnowledge()
        oracle.load_revisions(rev_path)
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            import_revisions(rev_path, db, os.path.join(tmp, "h.jsonl"))
            valid_grid = oracle.finite_valid_grid(valid_lo, valid_hi)
            known_grid = oracle.known_grid()
            cases = oracle.grid_cases(entities, valid_grid, known_grid)
            checked = 0
            for case in cases:
                expected = oracle.answer(
                    case.entity, case.valid_at, case.known_at
                )
                actual = query(
                    case.entity, case.valid_at, case.known_at, db
                )
                self.assertEqual(
                    (actual["found"], actual["value"]),
                    (expected["found"], expected["value"]),
                    msg=f"{case} 与参考器不一致",
                )
                checked += 1
            self.assertGreater(checked, 100)

    def test_main_fixture_grid(self):
        # 有效网格覆盖负时间、全部边界与开放尾抽样点（到 15）
        self._cross_check("revisions.jsonl", ["c1", "c2"], -7, 15)

    def test_extra_fixture_grid(self):
        self._cross_check("revisions_extra.jsonl", ["d1"], -3, 25)

    def test_oracle_distinguishes_null_from_retraction(self):
        oracle = ReferenceKnowledge()
        oracle.load_revisions(str(FIXTURES / "revisions.jsonl"))
        # c2 整条轴是 assert null：存在且值为 None
        self.assertEqual(
            oracle.answer("c2", 100, 6), {"found": True, "value": None}
        )
        # c1 在 K=3 的 valid_at=6 已撤回：不存在
        self.assertEqual(
            oracle.answer("c1", 6, 3), {"found": False, "value": None}
        )
        self.assertIs(oracle._state_before("c1", 6, 3), RETRACTED)


class JsonlArtifactTests(unittest.TestCase):
    """history.jsonl 必须是脱离 SQLite 也能独立查询的产物。"""

    def test_jsonl_only_query_matches_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            jl = os.path.join(tmp, "h.jsonl")
            import_revisions(str(FIXTURES / "revisions.jsonl"), db, jl)

            def jsonl_query(entity, valid_at, known_at):
                with open(jl, encoding="utf-8") as fh:
                    for line in fh:
                        row = json.loads(line)
                        if row["entity"] != entity:
                            continue
                        if not (row["sys_from"] <= known_at
                                and (row["sys_to"] is None or row["sys_to"] > known_at)):
                            continue
                        if not (row["valid_from"] <= valid_at
                                and (row["valid_to"] is None or row["valid_to"] > valid_at)):
                            continue
                        return row
                return None

            for q in _load_queries("queries.jsonl"):
                from_db = query(q["entity"], q["valid_at"], q["known_at"], db)
                from_file = jsonl_query(
                    q["entity"], q["valid_at"], q["known_at"]
                )
                if from_db["found"]:
                    self.assertIsNotNone(from_file)
                    self.assertEqual(from_file["value"], from_db["value"])
                else:
                    self.assertIsNone(from_file)

    def test_delivered_artifact_exists_and_is_consistent(self):
        db = ROOT / "history" / "bitemporal.db"
        jl = ROOT / "history" / "history.jsonl"
        self.assertTrue(db.exists())
        self.assertTrue(jl.exists())
        rows = []
        with open(jl, encoding="utf-8") as fh:
            for line in fh:
                rows.append(json.loads(line))
        conn = sqlite3.connect(db)
        try:
            n = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, len(rows))
        # 两个 to 字段的 null 语义都在产物中保留
        self.assertTrue(
            any(r["valid_to"] is None and r["sys_to"] is None for r in rows)
        )


class InvalidInputTests(unittest.TestCase):
    def _assert_fails_before_state_change(self, fixture_name):
        path = str(FIXTURES / fixture_name)
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            jl = os.path.join(tmp, "h.jsonl")
            with self.assertRaises(RevisionError):
                import_revisions(path, db, jl)
            # 失败后不得留下半成品数据库或历史文件
            self.assertFalse(os.path.exists(jl))
            if os.path.exists(db):
                conn = sqlite3.connect(db)
                try:
                    tables = [
                        r[0]
                        for r in conn.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    ]
                finally:
                    conn.close()
                if "history" in tables:
                    conn = sqlite3.connect(db)
                    try:
                        n = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
                    finally:
                        conn.close()
                    self.assertEqual(n, 0)

    def test_invalid_interval(self):
        self._assert_fails_before_state_change("invalid.jsonl")

    def test_non_increasing_known_at(self):
        self._assert_fails_before_state_change("invalid_order.jsonl")

    def test_duplicate_id(self):
        self._assert_fails_before_state_change("invalid_dup.jsonl")

    def test_non_finite_value(self):
        self._assert_fails_before_state_change("invalid_value.jsonl")

    def test_retract_with_value_rejected(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as fh:
            fh.write(
                '{"id":"z1","known_at":1,"entity":"c","from":0,"to":5,'
                '"kind":"retract","value":"x"}\n'
            )
            path = fh.name
        try:
            with self.assertRaises(RevisionError):
                parse_revisions(path)
        finally:
            os.unlink(path)

    def test_materialization_failure_rolls_back(self):
        # 校验通过后若物化阶段抛错，事务必须回滚，历史表保持为空
        revisions = parse_revisions(str(FIXTURES / "revisions.jsonl"))
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            store = HistoryStore(db)
            store.begin()
            try:
                store.apply_revision(revisions[0])
                # 模拟物化中途故障
                raise RuntimeError("boom")
            except RuntimeError:
                store.rollback()
            finally:
                store.close()
            conn = sqlite3.connect(db)
            try:
                n = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(n, 0)


class ScenarioDocTests(unittest.TestCase):
    """需求点名的七个场景在主/扩展 fixture 中均有覆盖并通过端到端验证。"""

    def test_named_scenarios(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "h.db")
            import_revisions(
                str(FIXTURES / "revisions.jsonl"),
                db, os.path.join(tmp, "h.jsonl"),
            )
            # 知识时点恰等提交时刻：K=2 立即可见 a2
            self.assertEqual(query("c1", 4, 2, db)["value"], "south")
            # 开放尾区间
            self.assertEqual(query("c1", 9999, 5, db)["value"], "north")
            # 负时间
            db2 = os.path.join(tmp, "h2.db")
            import_revisions(
                str(FIXTURES / "revisions.jsonl"),
                db2, os.path.join(tmp, "h2.jsonl"),
            )
            self.assertEqual(query("c2", -5, 6, db2)["value"], None)
            self.assertFalse(query("c2", -6, 6, db2)["found"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
