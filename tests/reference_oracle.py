"""独立参考器：用最朴素的逐点重放回答网格上的查询。

与生产实现刻意不共享任何分段/合并代码：它只在每个整数有效时间点上
保存“当前已知值”，逐条修订直接改写被覆盖的点，查询时直接读字典。
生产历史表在同一有限整数网格上的每个点都必须与参考器一致。

状态语义：

* 键不存在（或标记为 RETRACTED）表示该有效点没有事实；
* ``None`` 是一个真实存在的事实值（assert value=null）；
* assert 覆盖为标量（含 None），retract 覆盖为 RETRACTED。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

RETRACTED = object()


@dataclass
class GridPoint:
    entity: str
    valid_at: int
    known_at: int


class ReferenceKnowledge:
    """朴素双时态参考知识库。"""

    def __init__(self) -> None:
        # (entity, valid_at, known_at) -> 标量值 或 RETRACTED
        self._points: dict[tuple[str, int, int], Any] = {}
        self._revisions: list[dict[str, Any]] = []

    def load_revisions(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                text = line.strip()
                if text:
                    self._revisions.append(json.loads(text))

    def _state_before(self, entity: str, valid_at: int, known_at: int) -> Any:
        """重放 known_at 及之前到达的修订，得到该点在切片 K 下的状态。

        系统时间语义为半开区间 ``[sys_from, sys_to)``，因此提交时刻
        恰等于 K 的修订在切片 K 中已经可见（``sys_from <= K``）。
        """

        state: Any = RETRACTED  # 输入前整个时间轴都没有事实
        for rev in self._revisions:
            if rev["known_at"] > known_at or rev["entity"] != entity:
                continue
            lo, hi = rev["from"], rev["to"]
            if lo <= valid_at and (hi is None or valid_at < hi):
                if rev["kind"] == "assert":
                    state = rev["value"]
                else:
                    state = RETRACTED
        return state

    def answer(self, entity: str, valid_at: int, known_at: int) -> dict[str, Any]:
        """参考器点查询，返回与生产 query() 相同形态的结果（不含 row）。"""

        state = self._state_before(entity, valid_at, known_at)
        if state is RETRACTED:
            return {"found": False, "value": None}
        return {"found": True, "value": state}

    def grid_cases(
        self,
        entities: list[str],
        valid_grid: list[int],
        known_grid: list[int],
    ) -> list[GridPoint]:
        """枚举 entity × 有效时间网格 × 知识时刻网格的全部查询点。"""

        return [
            GridPoint(entity=e, valid_at=v, known_at=k)
            for e in entities
            for k in known_grid
            for v in valid_grid
        ]

    def finite_valid_grid(self, lo: int, hi: int) -> list[int]:
        """闭区间整数有效时间网格（to=null 的开放尾由 hi 点抽样代表）。"""

        return list(range(lo, hi + 1))

    def known_grid(self, extra: Optional[list[int]] = None) -> list[int]:
        """知识时刻网格：每个提交时刻本身 + 提交间隙点 + 额外边界点。

        包含提交时刻是为了验证“K 恰等提交时刻即可见”（sys_from <= K）。
        """

        points = {0}
        for rev in self._revisions:
            k = rev["known_at"]
            points.add(k)
            points.add(k - 1)
            points.add(k + 1)
        if extra:
            points.update(extra)
        return sorted(p for p in points if p >= 0)
