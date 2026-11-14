"""EPUB3 正文与脚注抽取。

仅使用标准库（zipfile / xml.etree / urllib），面向小型文本 EPUB3：
按 container -> OPF manifest/spine 的真实阅读顺序产出 h1/h2/p 正文单位，
独立导出 epub:type 含 footnote 的 aside 脚注及其 noteref 引用关系。
"""

from .model import (
    ExtractionResult,
    Footnote,
    NoteRef,
    RefError,
    TextUnit,
)
from .extractor import ExtractionError, extract_epub

__all__ = [
    "ExtractionError",
    "ExtractionResult",
    "Footnote",
    "NoteRef",
    "RefError",
    "TextUnit",
    "extract_epub",
]
