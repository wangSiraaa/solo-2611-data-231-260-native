"""EPUB3 抽取主逻辑。

流程：META-INF/container.xml 定位 OPF -> 解释 manifest -> 按 spine 的
itemref 顺序处理 linear=yes/未声明的 XHTML 文档 -> 树序收集 h1/h2/p，
aside[epub:type~=footnote] 仅进入 notes，noteref 锚点文本不进正文。
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from .model import ExtractionResult, Footnote, NoteRef, RefError, TextUnit

NS_CONTAINER = "urn:oasis:names:tc:opendocument:xmlns:container"
NS_OPF = "http://www.idpf.org/2007/opf"
NS_DC = "http://purl.org/dc/elements/1.1/"
NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_EPUB = "http://www.idpf.org/2007/ops"
NS_XML = "http://www.w3.org/XML/1998/namespace"  # xml: 前缀由 XML 规范固定绑定

BODY_UNIT_TAGS = {f"{{{NS_XHTML}}}h1", f"{{{NS_XHTML}}}h2", f"{{{NS_XHTML}}}p"}
SKIP_TAGS = {f"{{{NS_XHTML}}}script", f"{{{NS_XHTML}}}style"}
FOOTNOTE_TAG = f"{{{NS_XHTML}}}aside"

XHTML_MEDIA_TYPE = "application/xhtml+xml"
# HTML 规范中的 ASCII 空白字符（全角空格 U+3000 不在内，按普通字符保留）
_WS_RUN = re.compile(r"[ \t\n\f\r\v]+")


class ExtractionError(Exception):
    """致命错误：未知 spine idref、不支持的正文媒体类型、结构损坏等，整本拒绝。"""


@dataclass
class _Doc:
    """已解析的归档内 XHTML 文档。"""

    path: str
    root: ET.Element
    by_id: dict[str, ET.Element]
    tree_pos: dict[ET.Element, tuple[int, ...]]


@dataclass
class _Segment:
    """内联遍历产物：文本片段或 noteref 标记。"""

    text: str = ""
    ref: NoteRef | None = None
    ref_element: ET.Element | None = None
    base: str = ""  # 该标记所在位置继承到的 xml:base（含自身声明）


def _norm_path(path: str) -> str:
    """把归档内相对路径规范成 POSIX 形式；禁止跳出归档根。"""
    normalized = posixpath.normpath(path)
    if normalized.startswith("../") or normalized == ".." or normalized.startswith("/"):
        raise ExtractionError(f"归档资源路径越界: {path!r}")
    return normalized


def _join_base(base_doc: str, href: str) -> tuple[str, str]:
    """在归档命名空间内解析相对 href，返回 (规范文档路径, fragment)。

    base_doc 既可能是文档路径（取其目录）也可能是以 / 结尾的 xml:base 目录；
    外部 URL 直接抛出，由调用方记诊断；绝不经过操作系统文件遍历。
    """
    scheme = urllib.parse.urlsplit(href).scheme.lower()
    if scheme:
        # 任何带 scheme 的 URI（http/https/mailto/file/data/...）都不按
        # 归档内相对引用处理；归档内资源只允许无 scheme 的相对路径
        raise _External(href)

    split = urllib.parse.urlsplit(href)
    fragment = urllib.parse.unquote(split.fragment)
    rel_path = urllib.parse.unquote(split.path)
    if split.query:
        rel_path = rel_path + "?" + split.query

    if base_doc.endswith("/"):
        base_dir = base_doc
    else:
        base_dir = posixpath.dirname(base_doc)
    if rel_path:
        resolved = posixpath.normpath(posixpath.join(base_dir, rel_path))
    else:
        # 纯 fragment 引用：留在当前文档
        resolved = (
            posixpath.normpath(base_doc)
            if not base_doc.endswith("/")
            else base_doc.rstrip("/")
        )
    # normpath 已折叠字面 ../；这里拦截仍残留的越界前缀与绝对路径。
    # 归档根内的资源（如 texts/ 上两级回到根）仍属归档命名空间，允许访问。
    if resolved.startswith("../") or resolved == ".." or resolved.startswith("/"):
        raise ExtractionError(f"归档资源路径越界: {href!r} 相对 {base_doc!r}")
    return resolved, fragment


class _External(Exception):
    def __init__(self, href: str):
        super().__init__(href)
        self.href = href


def _epub_types(element: ET.Element) -> set[str]:
    raw = element.get(f"{{{NS_EPUB}}}type") or ""
    return {t for t in raw.split() if t}


def _is_footnote(element: ET.Element) -> bool:
    return element.tag == FOOTNOTE_TAG and "footnote" in _epub_types(element)


def _is_noteref(element: ET.Element) -> bool:
    return element.tag == f"{{{NS_XHTML}}}a" and "noteref" in _epub_types(element)


def _collapse(segments: list[_Segment]) -> tuple[str, dict[int, int]]:
    """按 scope 规则折叠空白。

    拼接符合条件的内联文本（DOM 顺序），HTML 空白连续段折叠为一个 ASCII
    空格，修剪首尾；noteref 标记贡献零长度。返回规范文本与“段序号 ->
    规范文本中的 Unicode 码点偏移”映射。
    """
    out: list[str] = []
    marker_at: dict[int, int] = {}
    pending_space = False
    started = False

    for idx, seg in enumerate(segments):
        if seg.ref is not None:
            # 标记零长度；记录此刻已确定的码点长度，悬挂空白由后续字符决定
            marker_at[idx] = len("".join(out))
            continue
        text = _WS_RUN.sub(" ", seg.text)
        for ch in text:
            if ch == " ":
                if started:
                    pending_space = True
            else:
                if pending_space:
                    out.append(" ")
                    pending_space = False
                out.append(ch)
                started = True
    canonical = "".join(out)
    return canonical, marker_at


class _Extractor:
    def __init__(self, source: str | zipfile.ZipFile):
        if isinstance(source, zipfile.ZipFile):
            self.zf = source
            self._owns = False
        else:
            self.zf = zipfile.ZipFile(source)
            self._owns = True
        self.docs: dict[str, _Doc] = {}
        self.notes: dict[str, Footnote] = {}  # target(path#id) -> 脚注实体
        self.note_order_counter = 0
        self.errors: list[RefError] = []
        self._summary_title: str | None = None
        self._summary_identifier: str | None = None

    # ---------- 归档与 XML ----------

    def _read_xml(self, name: str) -> ET.Element:
        try:
            raw = self.zf.read(name)
        except KeyError:
            raise ExtractionError(f"归档中缺少资源: {name}")
        try:
            return ET.fromstring(raw)
        except ET.ParseError as exc:
            raise ExtractionError(f"XML 解析失败 {name}: {exc}") from exc

    def _load_doc(self, path: str) -> _Doc:
        if path in self.docs:
            return self.docs[path]
        root = self._read_xml(path)
        by_id: dict[str, ET.Element] = {}
        tree_pos: dict[ET.Element, tuple[int, ...]] = {}

        def walk(node: ET.Element, position: tuple[int, ...]) -> None:
            node_id = node.get("id")
            if node_id is not None:
                by_id[node_id] = node
            tree_pos[node] = position
            for i, child in enumerate(list(node)):
                walk(child, position + (i,))

        walk(root, ())
        doc = _Doc(path=path, root=root, by_id=by_id, tree_pos=tree_pos)
        self.docs[path] = doc
        return doc

    # ---------- container / OPF ----------

    def _locate_opf(self) -> str:
        container = self._read_xml("META-INF/container.xml")
        rootfile = container.find(f".//{{{NS_CONTAINER}}}rootfile")
        if rootfile is None:
            raise ExtractionError("container.xml 缺少 rootfile")
        opf_href = rootfile.get("full-path")
        if not opf_href:
            raise ExtractionError("rootfile 缺少 full-path")
        return _norm_path(urllib.parse.unquote(opf_href))

    def _parse_opf(self, opf_path: str):
        opf = self._read_xml(opf_path)
        opf_base = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""

        metadata = opf.find(f"{{{NS_OPF}}}metadata")
        summary: dict[str, object] = {"source_opf": opf_path}
        if metadata is not None:
            title = metadata.find(f"{{{NS_DC}}}title")
            identifier = metadata.find(f"{{{NS_DC}}}identifier")
            language = metadata.find(f"{{{NS_DC}}}language")
            summary["title"] = title.text.strip() if title is not None and title.text else None
            summary["identifier"] = (
                identifier.text.strip() if identifier is not None and identifier.text else None
            )
            summary["language"] = (
                language.text.strip() if language is not None and language.text else None
            )

        manifest: dict[str, dict[str, str]] = {}
        manifest_el = opf.find(f"{{{NS_OPF}}}manifest")
        if manifest_el is None:
            raise ExtractionError("OPF 缺少 manifest")

        def inherited_opf_base(el: ET.Element, parent_map: dict[ET.Element, ET.Element]) -> str:
            """沿 OPF 父链累计 xml:base，得到 el 继承到的基准目录。"""
            bases = [opf_base]
            chain: list[ET.Element] = []
            cur = el
            while cur in parent_map:
                cur = parent_map[cur]
                chain.append(cur)
            for anc in reversed(chain):
                base = anc.get(f"{{{NS_XML}}}base")
                if base:
                    resolved = _join_base(bases[-1] or "", base)[0]
                    bases.append(resolved if resolved.endswith("/") else resolved + "/")
            return bases[-1]

        parent_map = {c: p for p in opf.iter() for c in p}
        for item in manifest_el.findall(f"{{{NS_OPF}}}item"):
            item_id = item.get("id")
            href = item.get("href")
            media_type = item.get("media-type", "")
            if not item_id or not href:
                raise ExtractionError("manifest item 缺少 id 或 href")
            base = inherited_opf_base(item, parent_map)
            own_base = item.get(f"{{{NS_XML}}}base")
            if own_base:
                resolved = _join_base(base, own_base)[0]
                base = resolved if resolved.endswith("/") else resolved + "/"
            doc_path, fragment = _join_base(base, urllib.parse.unquote(href))
            if fragment:
                raise ExtractionError(f"manifest item {item_id!r} 的 href 不应带 fragment")
            manifest[item_id] = {
                "path": doc_path,
                "media_type": media_type,
                "properties": item.get("properties", ""),
            }

        spine_el = opf.find(f"{{{NS_OPF}}}spine")
        if spine_el is None:
            raise ExtractionError("OPF 缺少 spine")
        spine: list[tuple[str, bool]] = []  # (manifest id, linear)
        for itemref in spine_el.findall(f"{{{NS_OPF}}}itemref"):
            idref = itemref.get("idref")
            if not idref:
                raise ExtractionError("spine itemref 缺少 idref")
            if idref not in manifest:
                # 未知 spine idref：明确拒绝，不静默跳章
                raise ExtractionError(f"spine 引用了 manifest 中不存在的 idref: {idref!r}")
            linear_attr = itemref.get("linear")
            linear = True if linear_attr is None else linear_attr.strip().lower() == "yes"
            if linear_attr is not None and linear_attr.strip().lower() not in ("yes", "no"):
                raise ExtractionError(f"itemref {idref!r} 的 linear 取值非法: {linear_attr!r}")
            spine.append((idref, linear))

        return manifest, spine, summary

    # ---------- 引用解析 ----------

    def _make_ref(
        self,
        element: ET.Element,
        source_doc: _Doc,
        source_node_id: str | None,
        source_kind: str,
        insertion_index: int,
        ref_stack: list[str],
        current_base: str,
    ) -> NoteRef:
        href = element.get("href", "")
        anchor_text = "".join(element.itertext())
        ref = NoteRef(
            target="",
            href=href,
            anchor_text=anchor_text,
            source_doc=source_doc.path,
            source_node_id=source_node_id,
            source_kind=source_kind,
            insertion_index=insertion_index,
        )
        if not href.strip():
            ref.target = ""
            ref.error = RefError(
                "missing_target",
                href,
                source_doc.path,
                source_node_id,
                source_kind,
                "noteref 缺少 href，无法定位脚注目标",
            )
            self.errors.append(ref.error)
            return ref
        try:
            doc_path, fragment = _join_base(current_base, href)
        except _External as exc:
            ref.target = href
            ref.error = RefError(
                "external_reference",
                href,
                source_doc.path,
                source_node_id,
                source_kind,
                f"外部 URL 不能作为脚注目标: {exc.href!r}",
            )
            self.errors.append(ref.error)
            return ref

        target = f"{doc_path}#{fragment}"
        ref.target = target
        if doc_path not in self.zf.namelist():
            ref.error = RefError(
                "missing_target",
                href,
                source_doc.path,
                source_node_id,
                source_kind,
                f"目标文档在归档中不存在: {doc_path}",
            )
            self.errors.append(ref.error)
            return ref
        try:
            doc = self._load_doc(doc_path)
        except ExtractionError as exc:
            ref.error = RefError(
                "missing_target",
                href,
                source_doc.path,
                source_node_id,
                source_kind,
                f"目标文档无法作为 XHTML 解析: {doc_path}（{exc}）",
            )
            self.errors.append(ref.error)
            return ref

        target_el = doc.by_id.get(fragment) if fragment else None
        if fragment and target_el is None:
            ref.error = RefError(
                "missing_target",
                href,
                source_doc.path,
                source_node_id,
                source_kind,
                f"目标片段在 {doc_path} 中不存在: #{fragment}",
            )
            self.errors.append(ref.error)
            return ref
        if not fragment or not _is_footnote(target_el):
            ref.error = RefError(
                "not_a_footnote",
                href,
                source_doc.path,
                source_node_id,
                source_kind,
                f"目标 {target} 不是 epub:type 含 footnote 的 aside",
            )
            self.errors.append(ref.error)
            return ref
        if target in ref_stack:
            ref.error = RefError(
                "reference_cycle",
                href,
                source_doc.path,
                source_node_id,
                source_kind,
                f"脚注引用环: {' -> '.join(ref_stack + [target])}",
            )
            self.errors.append(ref.error)
            return ref

        # 有效引用：按需取得共享脚注实体
        note = self._get_note(target, doc, target_el, ref_stack)
        source_identity = f"{source_doc.path}#{source_node_id}" if source_node_id else source_doc.path
        note.referrers.append(source_identity)
        return ref

    def _get_note(
        self,
        target: str,
        doc: _Doc,
        element: ET.Element,
        ref_stack: list[str],
    ) -> Footnote:
        if target in self.notes:
            return self.notes[target]
        self.note_order_counter += 1
        note = Footnote(
            note_order=self.note_order_counter,
            target=target,
            doc=doc.path,
            node_id=element.get("id"),
            tree_position=doc.tree_pos[element],
            canonical_text="",
        )
        self.notes[target] = note  # 先登记，环回来时实体已存在
        ref_stack = ref_stack + [target]
        units, refs, canonical = self._collect_block_units(
            doc, element, ref_stack, note_context=note
        )
        note.units = units
        note.refs = refs
        note.canonical_text = canonical
        note.summary = {
            "title": self._summary_title,
            "identifier": self._summary_identifier,
            "doc": doc.path,
            "target": target,
        }
        return note

    # ---------- xml:base ----------

    def _push_base(self, base_stack: list[str], base: str) -> None:
        resolved = _join_base(base_stack[-1], base)[0]
        if not resolved.endswith("/"):
            resolved += "/"
        base_stack.append(resolved)

    def _inherited_base_stack(self, doc: _Doc, element: ET.Element) -> list[str]:
        """累计 element 的严格祖先（含文档根，不含 element 自身）的 xml:base。

        xml:base 按 DOM 祖先链继承；初始 base 是文档自身的归档路径。
        根节点自带的 xml:base 会影响其后代（如 body 内 p 中的 noteref），
        但不改变文档自身在归档中的定位。element 自身的 base 由调用方再压栈。
        """
        position = doc.tree_pos.get(element, ())
        node = doc.root
        bases = [doc.path]
        # position 是相对 root 的下标路径；走到 element 的父节点为止，
        # 途中每个祖先（含 root）的 xml:base 都参与继承
        for index in position:
            base = node.get(f"{{{NS_XML}}}base")
            if base:
                self._push_base(bases, base)
            node = list(node)[index]
        return bases

    # ---------- 内联遍历 ----------

    def _collect_inline(
        self,
        container: ET.Element,
        doc: _Doc,
        source_node_id: str | None,
        source_kind: str,
        ref_stack: list[str],
        note_context: Footnote | None,
    ) -> tuple[str, list[NoteRef]]:
        segments: list[_Segment] = []
        base_stack = self._inherited_base_stack(doc, container)

        def walk_inline(node: ET.Element) -> None:
            if _is_noteref(node):
                # 锚点文本不计入规范文本；记录解析时刻的继承 base 与插入占位
                placeholder = NoteRef(
                    target="",
                    href=node.get("href", ""),
                    anchor_text="".join(node.itertext()),
                    source_doc=doc.path,
                    source_node_id=source_node_id,
                    source_kind=source_kind,
                    insertion_index=-1,
                )
                segments.append(
                    _Segment(ref=placeholder, ref_element=node, base=base_stack[-1])
                )
                return  # 锚点子树不再展开
            if node.text:
                segments.append(_Segment(text=node.text))
            for child in list(node):
                if child.tag in SKIP_TAGS:
                    if child.tail:
                        segments.append(_Segment(text=child.tail))
                    continue
                pushed = False
                base = child.get(f"{{{NS_XML}}}base")
                if base:
                    self._push_base(base_stack, base)
                    pushed = True
                walk_inline(child)
                if pushed:
                    base_stack.pop()
                if child.tail:
                    segments.append(_Segment(text=child.tail))

        # 容器自身的 xml:base 也参与其内部 href 解析
        own_base = container.get(f"{{{NS_XML}}}base")
        if own_base:
            self._push_base(base_stack, own_base)
        if container.text:
            segments.append(_Segment(text=container.text))
        for child in list(container):
            if child.tag in SKIP_TAGS:
                if child.tail:
                    segments.append(_Segment(text=child.tail))
                continue
            pushed = False
            base = child.get(f"{{{NS_XML}}}base")
            if base:
                self._push_base(base_stack, base)
                pushed = True
            walk_inline(child)
            if pushed:
                base_stack.pop()
            if child.tail:
                segments.append(_Segment(text=child.tail))
        if own_base:
            base_stack.pop()

        canonical, marker_at = _collapse(segments)
        refs: list[NoteRef] = []
        for idx, seg in enumerate(segments):
            if seg.ref is None:
                continue
            refs.append(
                self._make_ref(
                    seg.ref_element,  # type: ignore[arg-type]
                    doc,
                    source_node_id,
                    source_kind,
                    marker_at[idx],
                    ref_stack,
                    seg.base,
                )
            )
        return canonical, refs

    # ---------- 块级收集（正文 / 脚注共用） ----------

    def _collect_block_units(
        self,
        doc: _Doc,
        scope: ET.Element,
        ref_stack: list[str],
        note_context: Footnote | None,
    ) -> tuple[list[TextUnit], list[NoteRef], str]:
        units: list[TextUnit] = []
        all_refs: list[NoteRef] = []
        texts: list[str] = []

        def walk(node: ET.Element, depth: int) -> None:
            for child in list(node):
                if child.tag in SKIP_TAGS:
                    continue  # script/style 不产出任何文本
                if _is_footnote(child):
                    continue  # 脚注子树不进当前正文/脚注的单位序列
                if child.tag in BODY_UNIT_TAGS:
                    node_id = child.get("id")
                    canonical, refs = self._collect_inline(
                        child,
                        doc,
                        node_id,
                        "note" if note_context is not None else "body",
                        ref_stack,
                        note_context,
                    )
                    unit = TextUnit(
                        reading_index=-1,  # 正文单位在 spine 遍历阶段统一编号
                        tag=child.tag.split("}", 1)[1],
                        doc=doc.path,
                        node_id=node_id,
                        tree_position=doc.tree_pos[child],
                        canonical_text=canonical,
                        refs=refs,
                        summary={
                            "title": self._summary_title,
                            "identifier": self._summary_identifier,
                            "doc": doc.path,
                            "node_id": node_id,
                        },
                    )
                    units.append(unit)
                    all_refs.extend(refs)
                    texts.append(canonical)
                    continue  # h1/h2/p 内部不会再嵌套同名块单位
                walk(child, depth + 1)

        walk(scope, 0)
        return units, all_refs, "\n".join(t for t in texts if t)

    # ---------- 主流程 ----------

    def run(self) -> ExtractionResult:
        opf_path = self._locate_opf()
        manifest, spine, summary = self._parse_opf(opf_path)
        self._summary_title = summary.get("title")  # type: ignore[assignment]
        self._summary_identifier = summary.get("identifier")  # type: ignore[assignment]

        body: list[TextUnit] = []
        reading_index = 0
        spine_seq: list[dict[str, object]] = []

        for idref, linear in spine:
            entry = manifest[idref]
            spine_seq.append(
                {"idref": idref, "path": entry["path"], "linear": linear}
            )
            if not linear:
                continue  # linear=no 不自动进入正文（其媒体类型不按正文要求约束），
                # 但可被 noteref 按需加载；加载后若不是 XML 会在 _read_xml 处明确报错
            if entry["media_type"] != XHTML_MEDIA_TYPE:
                raise ExtractionError(
                    f"spine 正文资源 {idref!r} 媒体类型不受支持: "
                    f"{entry['media_type']!r}（本抽取器只接受 {XHTML_MEDIA_TYPE}）"
                )
            doc = self._load_doc(entry["path"])
            body_el = doc.root.find(f".//{{{NS_XHTML}}}body")
            if body_el is None:
                raise ExtractionError(f"{entry['path']} 缺少 XHTML body")
            units, _, _ = self._collect_block_units(
                doc, body_el, ref_stack=[], note_context=None
            )
            for unit in units:
                unit.reading_index = reading_index
                reading_index += 1
                body.append(unit)

        summary_out = dict(summary)
        summary_out["spine"] = spine_seq
        result = ExtractionResult(
            summary=summary_out,
            body=body,
            notes=list(self.notes.values()),
            errors=self.errors,
            complete=not self.errors,
        )
        if self._owns:
            self.zf.close()
        return result


def extract_epub(source: str | zipfile.ZipFile) -> ExtractionResult:
    """抽取 EPUB3，返回正文、脚注与诊断；致命结构问题抛 ExtractionError。"""
    return _Extractor(source).run()
