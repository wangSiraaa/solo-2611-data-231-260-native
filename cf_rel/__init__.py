"""CF trajectoryProfile NetCDF -> 关系表 (JSONL) 转换包。

只支持 trajectory -> profile -> observation 两层结构。所有父子关系均来自
文件中的索引变量 (trajectoryIndex + rowSize/profileIndex)，不做插值、
轨迹匹配或任何科学推断。
"""

from .converter import ConversionResult, convert_file
from .errors import ConversionError, UnsupportedFeatureError, ValidationError

__all__ = [
    "convert_file",
    "ConversionResult",
    "ConversionError",
    "ValidationError",
    "UnsupportedFeatureError",
]
