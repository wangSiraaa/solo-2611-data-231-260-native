"""抽取结果的数据结构。

阅读顺序、脚注出现顺序、引用在正文中的位置是三套互相独立的坐标，
分别落在 reading_index / note_order / insertion_index 上，不共用序号。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RefError:
    """一条引用诊断。坏引用仍登记在来源单位的 refs 中，绝不静默丢弃。"""

    kind: str  # missing_target | not_a_footnote | external_reference | reference_cycle
    href: str
    source_doc: str
    source_node_id: str | None
    source_kind: str  # body | note
    message: str


@dataclass
class NoteRef:
    """一次 noteref 引用。

    insertion_index 是 noteref 锚点文本在“规范正文”中的插入位置，
    以 Unicode 码点计；锚点自身的 [1] 之类文本不计入规范文本（零长度）。
    多个 noteref 指向同一脚注时共享 target，每次引用各自保留来源。
    """

    target: str  # 规范 ZIP 内部路径 + "#" + fragment，如 OEBPS/notes.xhtml#n1
    href: str  # 原始 href
    anchor_text: str  # 锚点内联文本，仅留档，不进规范正文
    source_doc: str
    source_node_id: str | None
    source_kind: str  # body | note
    insertion_index: int
    error: RefError | None = None


@dataclass
class TextUnit:
    """正文单位（仅限 h1/h2/p），按文档树顺序、spine 阅读顺序排列。"""

    reading_index: int
    tag: str
    doc: str  # 归档内规范路径
    node_id: str | None
    tree_position: tuple[int, ...]  # 在 XHTML 元素树中的位置路径
    canonical_text: str
    refs: list[NoteRef] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class Footnote:
    """脚注实体。同一目标只导出一次，所有引用进 referrers。"""

    note_order: int  # 脚注首次被引用的发现顺序，独立于 reading_index
    target: str  # path#id
    doc: str
    node_id: str | None
    tree_position: tuple[int, ...]
    canonical_text: str
    units: list[TextUnit] = field(default_factory=list)  # 脚注内部的 h1/h2/p
    refs: list[NoteRef] = field(default_factory=list)  # 脚注内再发起的引用
    referrers: list[str] = field(default_factory=list)  # 引用来源 path#id（或单位标识）
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractionResult:
    summary: dict[str, Any]
    body: list[TextUnit]
    notes: list[Footnote]
    errors: list[RefError]
    complete: bool

    def to_dict(self) -> dict[str, Any]:
        def ref(r: NoteRef) -> dict[str, Any]:
            return {
                "target": r.target,
                "href": r.href,
                "anchor_text": r.anchor_text,
                "source_doc": r.source_doc,
                "source_node_id": r.source_node_id,
                "source_kind": r.source_kind,
                "insertion_index": r.insertion_index,
                "error": None if r.error is None else vars(r.error),
            }

        def unit(u: TextUnit) -> dict[str, Any]:
            return {
                "reading_index": u.reading_index,
                "tag": u.tag,
                "doc": u.doc,
                "node_id": u.node_id,
                "tree_position": list(u.tree_position),
                "canonical_text": u.canonical_text,
                "refs": [ref(r) for r in u.refs],
                "summary": u.summary,
            }

        return {
            "summary": self.summary,
            "complete": self.complete,
            "body": [unit(u) for u in self.body],
            "notes": [
                {
                    "note_order": n.note_order,
                    "target": n.target,
                    "doc": n.doc,
                    "node_id": n.node_id,
                    "tree_position": list(n.tree_position),
                    "canonical_text": n.canonical_text,
                    "units": [unit(u) for u in n.units],
                    "refs": [ref(r) for r in n.refs],
                    "referrers": n.referrers,
                    "summary": n.summary,
                }
                for n in self.notes
            ],
            "errors": [vars(e) for e in self.errors],
        }
