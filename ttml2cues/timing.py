"""timing.md 允许的有限时间表达式解析。

只支持：
  * 非负十进制秒 ``12s`` / ``0.5s``
  * 非负十进制毫秒 ``250ms``
  * 整数 tick ``100t``
不支持 hh:mm:ss、frame、wallclock、动画等任何其他形式。

全部使用 decimal.Decimal 精确算术，tickRate 固定 1000；
不能换算成整数 tick 的表达式直接拒绝，绝不走浮点秒累计。
"""
from __future__ import annotations

import re
from decimal import Decimal

TICK_RATE = 1000

_TICK_RE = re.compile(r"^\d+t$")
_DECIMAL_RE = re.compile(r"^(\d+(?:\.\d+)?)(s|ms)$")


class TimingError(ValueError):
    """时间表达式或时钟层级不满足 timing.md 子集约束。"""


def parse_time_expression(expr: str, tick_rate: int = TICK_RATE) -> int:
    """把单个时间表达式精确换算成整数 tick。

    任何不支持的形式、或在给定 tickRate 下不是整数 tick 的值都抛 TimingError。
    """
    if not isinstance(expr, str):
        raise TimingError(f"时间表达式必须是字符串，得到 {expr!r}")
    text = expr.strip()
    if not text:
        raise TimingError("空时间表达式")

    if _TICK_RE.fullmatch(text):
        return int(text[:-1])

    match = _DECIMAL_RE.fullmatch(text)
    if match is None:
        raise TimingError(
            f"不支持的时间表达式 {expr!r}：子集只允许十进制 s/ms 与整数 t，"
            "不支持 hh:mm:ss、frame、wallclock 或动画"
        )

    value = Decimal(match.group(1))
    unit = match.group(2)
    # tickRate=1000 时 1s=1000 tick、1ms=1 tick；全程 Decimal，不引入 float。
    factor = Decimal(tick_rate) if unit == "s" else Decimal(1)
    ticks = value * factor
    if ticks != ticks.to_integral_value():
        raise TimingError(
            f"时间表达式 {expr!r} 在 tickRate={tick_rate} 下不是整数 tick，"
            "子集要求精确表示，拒绝转换"
        )
    return int(ticks)
