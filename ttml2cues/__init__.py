"""有限 TTML 字幕 -> 规范字幕事实 / 时间片 的转换包。

只实现 timing.md 声明的媒体时钟子集，不做渲染、布局或音视频处理。
"""
from .converter import TimingError, convert_file
from .timing import TICK_RATE, parse_time_expression

__all__ = [
    "TICK_RATE",
    "TimingError",
    "convert_file",
    "parse_time_expression",
]
