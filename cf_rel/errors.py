"""转换过程中的错误类型。

所有校验错误都携带源文件名、变量名以及相关索引，便于定位问题文件。
任何 ValidationError 都会在发布最终关系表之前抛出。
"""


class ConversionError(Exception):
    """转换相关错误的基类。"""


class ValidationError(ConversionError):
    """文件语义或索引校验失败：rowSize 总和、索引越界、cf_role 不符等。"""


class UnsupportedFeatureError(ConversionError):
    """遇到本转换器明确不支持的特性（如非 standard 日历、多维变量）。"""
