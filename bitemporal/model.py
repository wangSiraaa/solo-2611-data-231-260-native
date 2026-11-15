"""有效时间轴上的规范化分段（纯函数，不含存储与 IO）。

输入修订与旧的完整规范段列表，输出新的完整规范段列表。
规范列表的不变量：

* 段按 ``valid_from`` 排序，半开区间 ``[lo, hi)`` 互不重叠；
* 相邻段之间可能存在空隙（空隙代表该有效期没有事实）；
* 相邻且值相等的段必须合并（因此相邻段值必不相等）；
* 只保存存在事实的段，撤回只是把对应区间从列表中删除，
  绝不以一条 ``value=None`` 的段替代“不存在”；
* ``hi`` 为 ``None`` 表示正无穷，且只能出现在最后一段。

值的相等规则按需求定制：数值 1 与 1.0 相等；布尔与数值永不相等；
字符串按码点比较；``None`` 只与 ``None`` 相等。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


def values_equal(a: Any, b: Any) -> bool:
    """双时态事实值的相等规则。

    * 两个布尔：按布尔值比较；
    * 布尔与数值（含另一布尔）永不相等；
    * 两个数值：数值相等（``1 == 1.0``）；
    * 其余 JSON 标量（字符串、None）：按 Python 语义比较。
    """

    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a == b


def is_finite_scalar(value: Any) -> bool:
    """value 是否为允许的有限 JSON 标量。

    允许：有限数值、字符串、布尔、None。
    拒绝：NaN/Infinity、列表、字典及其他对象。
    """

    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, (int, float)):
        # bool 已在上面处理；这里排除 NaN 与无穷大
        return value == value and value not in (float("inf"), float("-inf"))
    return False


@dataclass
class Segment:
    """有效时间轴上的一个事实段：``[lo, hi)`` 内事实值为 ``value``。"""

    lo: int
    hi: Optional[int]
    value: Any

    def key(self) -> tuple[int, Optional[int], Any]:
        """用于规范化段之间的身份判定（边界 + 定制值相等）。"""

        return (self.lo, self.hi, self.value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Segment):
            return NotImplemented
        return (
            self.lo == other.lo
            and self.hi == other.hi
            and values_equal(self.value, other.value)
        )

    def __hash__(self) -> int:
        # dataclass 默认 hash 用 tuple，会把 True/1 混为一谈，这里显式区分
        if isinstance(self.value, bool):
            v_hash = hash(("__bool__", self.value))
        elif isinstance(self.value, (int, float)):
            v_hash = hash(("__number__", float(self.value)))
        elif isinstance(self.value, str):
            v_hash = hash(("__str__", self.value))
        else:  # None
            v_hash = hash(("__none__",))
        return hash((self.lo, self.hi, v_hash))


@dataclass(frozen=True)
class Revision:
    """一条按 known_at 严格递增到达的修订。"""

    rev_id: Any
    entity: str
    known_at: int
    lo: int
    hi: Optional[int]
    kind: str
    value: Any = None


def _split_remnants(
    old: Segment, lo: int, hi: Optional[int]
) -> list[Segment]:
    """旧段被修订区间 ``[lo, hi)`` 覆盖后，区间外保留下来的部分。"""

    remnants: list[Segment] = []
    if old.lo < lo:
        remnants.append(Segment(old.lo, lo, old.value))
    if hi is not None:
        if old.hi is None or old.hi > hi:
            remnants.append(Segment(hi, old.hi, old.value))
    # hi 为 None（正无穷）时旧段右侧不可能还有残余
    return remnants


def _merge_adjacent(segments: list[Segment]) -> list[Segment]:
    """排序后合并相邻（端点相接）且值相等的段。"""

    if not segments:
        return []
    segments = sorted(segments, key=lambda s: (s.lo, s.hi is None, s.hi))
    merged = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        if prev.hi == seg.lo and values_equal(prev.value, seg.value):
            merged[-1] = Segment(prev.lo, seg.hi, prev.value)
        else:
            merged.append(seg)
    return merged


def apply_revision(
    old_segments: list[Segment], revision: Revision
) -> list[Segment]:
    """把一条修订作用于某 entity 的旧规范段列表，返回新规范段列表。

    * 修订区间之外的旧值原样保留；
    * 与修订区间相交的旧段在边界处被拆开，交叠部分被替换或删除；
    * assert 在修订区间上写入给定标量（允许 None），retract 则不留事实；
    * 最后合并相邻且值相等的段。
    """

    lo, hi = revision.lo, revision.hi
    kept: list[Segment] = []

    for seg in old_segments:
        # 不相交（旧段完全在修订区间左侧或右侧）：整段保留
        if seg.hi is not None and seg.hi <= lo:
            kept.append(seg)
            continue
        if hi is not None and seg.lo >= hi:
            kept.append(seg)
            continue
        # 相交：只保留修订区间外的残余，交叠部分由本条修订接管
        kept.extend(_split_remnants(seg, lo, hi))

    if revision.kind == "assert":
        kept.append(Segment(lo, hi, revision.value))
    # retract：修订区间不写入任何段（“没有事实”）

    return _merge_adjacent(kept)
