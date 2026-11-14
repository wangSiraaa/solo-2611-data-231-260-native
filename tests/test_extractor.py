"""EPUB3 抽取器验证。

关键约束：测试里的期望值不调用被测实现来生成，而是独立重新打开 ZIP、
独立解析 OPF spine、独立走 XHTML 树计算规范文本与 noteref 插入坐标，
再与抽取结果逐项核对。
"""

from __future__ import annotations

import json
import unittest
import zipfile
from xml.etree import ElementTree as ET

from epub_extract.extractor import NS_OPF, ExtractionError, extract_epub
from epub_extract.model import Footnote, TextUnit

from .epub_fixtures import NAV, XHTML_ITEMS, build_epub, opf

NS_XHTML = "http://www.w3.org/1999/xhtml"
NS_EPUB = "http://www.idpf.org/2007/ops"
NS_XML = "http://www.w3.org/XML/1998/namespace"
BOOK = "fixtures/book.epub"
BAD_SPINE = "fixtures/bad-spine.epub"


# ---------------------------------------------------------------------------
# 独立参考实现：不 import 被测代码的任何解析逻辑，重新取源节点核对
# ---------------------------------------------------------------------------


def _posix_join(base: str, rel: str) -> str:
    if base.endswith("/"):
        base_dir = base
    else:
        base_dir = base.rsplit("/", 1)[0] + "/" if "/" in base else ""
    import posixpath

    return posixpath.normpath(posixpath.join(base_dir, rel))


def independent_spine_order(epub_path: str) -> list[str]:
    """独立读 OPF，返回 linear 正文资源的归档路径顺序（忽略 nav 顺序）。"""
    with zipfile.ZipFile(epub_path) as zf:
        opf_path = ET.fromstring(zf.read("META-INF/container.xml")).find(
            ".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile"
        ).get("full-path")
        package = ET.fromstring(zf.read(opf_path))
        base_dir = opf_path.rsplit("/", 1)[0] + "/" if "/" in opf_path else ""
        manifest = {}
        for item in package.findall(f".//{{{NS_OPF}}}item"):
            manifest[item.get("id")] = base_dir + item.get("href")
        order = []
        for ref in package.findall(f".//{{{NS_OPF}}}itemref"):
            linear = ref.get("linear")
            if linear is not None and linear.lower() == "no":
                continue
            order.append(manifest[ref.get("idref")])
        return order


def independent_canonical(epub_path: str, doc_path: str, block_id: str):
    """独立重新打开文档，取指定 h1/h2/p 节点，计算规范文本与 noteref 坐标。

    与被测实现使用不同的遍历代码：这里用“标记占位 + 一次性折叠”，
    占位符用私用区码点 U+E000，折叠后按码点定位。
    """
    with zipfile.ZipFile(epub_path) as zf:
        root = ET.fromstring(zf.read(doc_path))

    target = None
    for el in root.iter():
        if el.get("id") == block_id:
            target = el
            break
    assert target is not None

    def inherited_base(el: ET.Element) -> str:
        # 重新构造父链（与生产代码不同的实现手段：建 parent 映射）
        parent = {c: p for p in root.iter() for c in p}
        bases = [doc_path]
        chain = []
        cur = el
        while cur in parent:
            cur = parent[cur]
            chain.append(cur)
        for anc in reversed(chain):
            b = anc.get(f"{{{NS_XML}}}base")
            if b:
                resolved = _posix_join(bases[-1], b) + "/"
                bases.append(resolved)
        own = el.get(f"{{{NS_XML}}}base")
        if own:
            bases.append(_posix_join(bases[-1], own) + "/")
        return bases[-1]

    MARK = ""
    pieces: list[str] = []
    markers: list[dict] = []

    def walk(el: ET.Element):
        tag = el.tag.split("}", 1)[-1]
        if tag in ("script", "style"):
            return
        if tag == "a" and "noteref" in (el.get(f"{{{NS_EPUB}}}type") or "").split():
            markers.append(
                {
                    "href": el.get("href"),
                    "anchor": "".join(el.itertext()),
                    "base": inherited_base(el),
                }
            )
            pieces.append(MARK)
            return
        if el.text:
            pieces.append(el.text)
        for child in list(el):
            walk(child)
            if child.tail:
                pieces.append(child.tail)

    walk(target)
    raw = "".join(pieces)
    collapsed_parts: list[str] = []
    marker_offsets: list[int] = []
    pending = False
    started = False
    for ch in raw:
        if ch == MARK:
            marker_offsets.append(len("".join(collapsed_parts)))
            continue
        if ch in " \t\r\n\f\v":
            if started:
                pending = True
        else:
            if pending:
                collapsed_parts.append(" ")
                pending = False
            collapsed_parts.append(ch)
            started = True
    canonical = "".join(collapsed_parts)
    return canonical, marker_offsets, markers


class BookEpubTests(unittest.TestCase):
    """对预置真实夹具 book.epub 的端到端核对。"""

    @classmethod
    def setUpClass(cls):
        cls.result = extract_epub(BOOK)
        with zipfile.ZipFile(BOOK) as zf:
            cls.zf_names = set(zf.namelist())

    def test_spine_order_not_nav_order(self):
        # 独立从 OPF 计算阅读顺序：spine 为 b,a；nav 故意为 a,b；
        # linear=no 的 notes 不进入正文序列
        expected = independent_spine_order(BOOK)
        self.assertEqual(
            expected,
            ["OEBPS/chapter-b.xhtml", "OEBPS/chapter-a.xhtml"],
        )
        actual_docs = [u.doc for u in self.result.body]
        # notes 是 linear=no，不出现在正文
        self.assertEqual(
            actual_docs,
            ["OEBPS/chapter-b.xhtml"] * 3 + ["OEBPS/chapter-a.xhtml"] * 3,
        )
        self.assertTrue(all(u.reading_index >= 0 for u in self.result.body))
        indices = [u.reading_index for u in self.result.body]
        self.assertEqual(indices, list(range(len(indices))))

    def test_linear_no_notes_doc_excluded_from_body(self):
        self.assertNotIn("OEBPS/notes.xhtml", {u.doc for u in self.result.body})
        spine = self.result.summary["spine"]
        notes_entry = next(s for s in spine if s["idref"] == "notes")
        self.assertFalse(notes_entry["linear"])

    def test_body_units_limited_to_h1_h2_p_tree_order(self):
        tags = [u.tag for u in self.result.body]
        self.assertEqual(tags, ["h1", "p", "p", "h1", "p", "p"])
        for unit in self.result.body:
            self.assertIn(unit.tag, ("h1", "h2", "p"))

    def test_independent_canonical_text_and_offsets(self):
        # 每个正文单位都独立重取源节点核对文字与 noteref 码点坐标
        for unit in self.result.body:
            if unit.node_id is None:
                continue
            canonical, offsets, markers = independent_canonical(
                BOOK, unit.doc, unit.node_id
            )
            self.assertEqual(
                unit.canonical_text,
                canonical,
                msg=f"规范文本不一致: {unit.doc}#{unit.node_id}",
            )
            self.assertEqual(len(unit.refs), len(markers))
            for ref, offset, marker in zip(unit.refs, offsets, markers):
                self.assertEqual(ref.insertion_index, offset)
                self.assertEqual(ref.href, marker["href"])
                self.assertEqual(ref.anchor_text, marker["anchor"])
                self.assertNotIn(marker["anchor"], unit.canonical_text)

    def test_noteref_anchor_text_never_body_word(self):
        for unit in self.result.body:
            for ref in unit.refs:
                self.assertFalse(ref.anchor_text.strip("[]").isdigit() and
                                 ref.anchor_text in unit.canonical_text)
        # 显式断言两个已知锚点 [1]/[?]/[L] 不进正文
        joined = "\n".join(u.canonical_text for u in self.result.body)
        for anchor in ("[1]", "[?]", "[L]"):
            self.assertNotIn(anchor, joined)

    def test_xml_base_resolution(self):
        # chapter-b 的 html 带 xml:base=texts/，../notes.xhtml 必须解析到 OEBPS/notes.xhtml
        p_b = next(u for u in self.result.body if u.node_id == "p-b")
        self.assertEqual(p_b.refs[0].target, "OEBPS/notes.xhtml#n1")
        self.assertIsNone(p_b.refs[0].error)
        # 同文档缺片段仍是 missing，但路径已正确解析到 OEBPS 下
        p_bad = next(u for u in self.result.body if u.node_id == "p-bad")
        self.assertEqual(p_bad.refs[0].target, "OEBPS/notes.xhtml#missing")
        self.assertEqual(p_bad.refs[0].error.kind, "missing_target")

    def test_shared_footnote_single_entity_multiple_referrers(self):
        n1 = [n for n in self.result.notes if n.target == "OEBPS/notes.xhtml#n1"]
        self.assertEqual(len(n1), 1)  # 跨章节共享一个实体
        note = n1[0]
        self.assertEqual(
            sorted(note.referrers),
            ["OEBPS/chapter-a.xhtml#p-a", "OEBPS/chapter-b.xhtml#p-b"],
        )
        # 每次引用都在各自来源单位里保留
        sources = [
            (r.source_doc, r.source_node_id)
            for u in self.result.body for r in u.refs
            if r.target == "OEBPS/notes.xhtml#n1" and r.error is None
        ]
        self.assertEqual(
            sorted(sources),
            [
                ("OEBPS/chapter-a.xhtml", "p-a"),
                ("OEBPS/chapter-b.xhtml", "p-b"),
            ],
        )

    def test_in_chapter_footnote_excluded_from_body_but_exported_as_note(self):
        local = [n for n in self.result.notes if n.target.endswith("#local")]
        self.assertEqual(len(local), 1)
        self.assertEqual(local[0].canonical_text, "本章注释")
        # aside 子树没有出现在任何正文单位里
        self.assertFalse(
            any("本章注释" in u.canonical_text for u in self.result.body)
        )

    def test_footnote_order_independent_from_reading_order(self):
        orders = [n.note_order for n in self.result.notes]
        self.assertEqual(orders, sorted(orders))
        self.assertEqual(orders, [1, 2, 3])
        # note_order 与 reading_index 取值空间分离：notes 上没有 reading_index 字段
        for note in self.result.notes:
            self.assertIsInstance(note, Footnote)
        for unit in self.result.body:
            self.assertIsInstance(unit, TextUnit)

    def test_reference_cycle_terminates_and_exports_once(self):
        n1 = next(n for n in self.result.notes if n.target.endswith("#n1"))
        n2 = next(n for n in self.result.notes if n.target.endswith("#n2"))
        # n1 -> n2 正常，n2 -> n1 报环
        self.assertEqual(len(n1.refs), 1)
        self.assertIsNone(n1.refs[0].error)
        self.assertEqual(len(n2.refs), 1)
        self.assertEqual(n2.refs[0].error.kind, "reference_cycle")
        # 环没有导致 n1 被重复导出
        self.assertEqual(len([n for n in self.result.notes if n.target.endswith("#n1")]), 1)
        cycle_errors = [e for e in self.result.errors if e.kind == "reference_cycle"]
        self.assertEqual(len(cycle_errors), 1)

    def test_completion_flag_and_error_publish_policy(self):
        # 有 missing + cycle，允许发布部分语料，但 complete 必须为 False
        self.assertFalse(self.result.complete)
        kinds = {e.kind for e in self.result.errors}
        self.assertEqual(kinds, {"missing_target", "reference_cycle"})
        # 坏引用仍登记在来源单位 refs 中，而不是被静默丢弃
        bad_refs = [r for u in self.result.body for r in u.refs if r.error is not None]
        self.assertTrue(bad_refs)

    def test_summary_traceability_fields(self):
        for unit in self.result.body:
            self.assertEqual(unit.summary["identifier"], "synthetic-book")
            self.assertEqual(unit.summary["doc"], unit.doc)
            self.assertEqual(unit.summary["node_id"], unit.node_id)
        for note in self.result.notes:
            self.assertEqual(note.summary["target"], note.target)
            self.assertEqual(note.summary["doc"], note.doc)

    def test_json_serializable(self):
        payload = json.dumps(self.result.to_dict(), ensure_ascii=False)
        decoded = json.loads(payload)
        self.assertEqual(len(decoded["body"]), len(self.result.body))
        self.assertEqual(len(decoded["notes"]), len(self.result.notes))


class BadSpineTests(unittest.TestCase):
    def test_unknown_idref_rejects_whole_book(self):
        with self.assertRaises(ExtractionError) as ctx:
            extract_epub(BAD_SPINE)
        self.assertIn("idref", str(ctx.exception))

    def test_no_success_corpus_published_on_bad_spine(self):
        with self.assertRaises(ExtractionError):
            extract_epub(BAD_SPINE)
        # CLI 层退出码为 2（在 test_cli 中另测），这里保证异常类型不含部分结果


class SyntheticRejectionTests(unittest.TestCase):
    """补充夹具：不支持的正文媒体类型必须整本拒绝。"""

    def test_unsupported_media_type_rejected(self, tmp_path=None):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            epub = Path(td) / "bad-media.epub"
            manifest = (
                '<item id="a" href="chapter-a.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="img" href="image.svg" media-type="image/svg+xml"/>'
                '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
            )
            spine = '<itemref idref="a"/><itemref idref="img"/>'
            build_epub(
                str(epub),
                {
                    "OEBPS/chapter-a.xhtml": _doc("<body><p>仅文字</p></body>"),
                    "OEBPS/image.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>",
                    "OEBPS/nav.xhtml": NAV,
                },
                opf(spine, manifest),
            )
            with self.assertRaises(ExtractionError) as ctx:
                extract_epub(str(epub))
            self.assertIn("媒体类型", str(ctx.exception))


class SyntheticReferenceTests(unittest.TestCase):
    """外部 URL、非脚注目标、noteref 边界位置、script/style、实体解码。"""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.epub = Path(self.tmp.name) / "ref.epub"
        chapter_a = _doc(
            """<body>
<h1 id="h1">标题&#65257;距</h1>
<p id="p-edge"><a id="r0" epub:type="noteref" href="notes.xhtml#n1">[1]</a>开头引用与结尾引用<a epub:type="noteref" href="notes.xhtml#n1">[1]</a></p>
<p id="p-ws">  多    个&#10;空白&#9;折叠  </p>
<p id="p-ext">外部<a epub:type="noteref" href="https://example.com/note#x">[ext]</a>链接</p>
<p id="p-nonfn">普通元素<a epub:type="noteref" href="#plain">[p]</a>目标</p>
<p id="p-script">前文<script>var x = 1;</script><style>a{}</style>后文</p>
<aside id="plain"><p>不是脚注</p></aside>
</body>"""
        )
        notes = _doc(
            """<body>
<aside id="n1" epub:type="footnote"><p id="np">脚注内容 &amp; 实体&#8364;</p></aside>
</body>"""
        )
        spine = '<itemref idref="a"/><itemref idref="notes" linear="no"/>'
        package = opf(spine, XHTML_ITEMS)
        build_epub(
            str(self.epub),
            {
                "OEBPS/chapter-a.xhtml": chapter_a,
                "OEBPS/notes.xhtml": notes,
                "OEBPS/nav.xhtml": NAV,
            },
            package,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_reference_diagnostics(self):
        result = extract_epub(str(self.epub))
        by_id = {u.node_id: u for u in result.body}

        ext = by_id["p-ext"].refs[0]
        self.assertEqual(ext.error.kind, "external_reference")
        nonfn = by_id["p-nonfn"].refs[0]
        self.assertEqual(nonfn.error.kind, "not_a_footnote")
        self.assertEqual(nonfn.target, "OEBPS/chapter-a.xhtml#plain")

        kinds = {e.kind for e in result.errors}
        self.assertIn("external_reference", kinds)
        self.assertIn("not_a_footnote", kinds)
        self.assertFalse(result.complete)

    def test_edge_marker_offsets_and_whitespace(self):
        result = extract_epub(str(self.epub))
        by_id = {u.node_id: u for u in result.body}

        edge = by_id["p-edge"]
        self.assertEqual(edge.canonical_text, "开头引用与结尾引用")
        self.assertEqual([r.insertion_index for r in edge.refs], [0, len("开头引用与结尾引用")])
        # 独立核对
        canonical, offsets, _ = independent_canonical(
            str(self.epub), "OEBPS/chapter-a.xhtml", "p-edge"
        )
        self.assertEqual(edge.canonical_text, canonical)
        self.assertEqual([r.insertion_index for r in edge.refs], offsets)

        ws = by_id["p-ws"]
        self.assertEqual(ws.canonical_text, "多 个 空白 折叠")

    def test_entities_decoded(self):
        result = extract_epub(str(self.epub))
        by_id = {u.node_id: u for u in result.body}
        # &#65257; = U+FEE9，核对数字字符引用已解码为对应码点
        self.assertEqual(by_id["h1"].canonical_text[2], chr(65257))
        note = next(n for n in result.notes if n.target.endswith("#n1"))
        self.assertIn("&", note.canonical_text)  # &amp;
        self.assertIn("€", note.canonical_text)  # &#8364;

    def test_script_style_produce_no_text(self):
        result = extract_epub(str(self.epub))
        by_id = {u.node_id: u for u in result.body}
        self.assertEqual(by_id["p-script"].canonical_text, "前文后文")
        joined = "\n".join(u.canonical_text for u in result.body)
        self.assertNotIn("var x", joined)
        self.assertNotIn("a{}", joined)

    def test_plain_aside_is_not_a_footnote(self):
        result = extract_epub(str(self.epub))
        self.assertFalse(any(n.node_id == "plain" for n in result.notes))


class NestedXmlBaseTests(unittest.TestCase):
    """多层继承 xml:base 与 h2 单位、脚注内再引用脚注的链（非环）。"""

    def setUp(self):
        import tempfile
        from pathlib import Path

        self.tmp = tempfile.TemporaryDirectory()
        self.epub = Path(self.tmp.name) / "nested.epub"
        # html 声明 base=texts/，内层 span 再声明 base=sub/，noteref 的
        # href=../endnotes.xhtml#e1 应解析为 OEBPS/texts/endnotes.xhtml
        chapter = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml"
      xmlns:epub="http://www.idpf.org/2007/ops" xml:base="texts/">
<head><title>t</title></head><body>
<h1 id="h1">章标题</h1>
<h2 id="h2">节标题</h2>
<p id="p1">正文<span xml:base="sub/">嵌套<a epub:type="noteref"
  href="../endnotes.xhtml#e1">[e]</a></span>收尾</p>
</body></html>"""
        endnotes = _doc(
            """<body>
<aside id="e1" epub:type="footnote">
<h2 id="eh">脚注里的标题</h2>
<p id="ep1">第一层<a epub:type="noteref" href="#e2">[2]</a></p>
</aside>
<aside id="e2" epub:type="footnote"><p id="ep2">第二层，无环终止</p></aside>
</body>"""
        )
        manifest = (
            '<item id="a" href="chapter.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="notes" href="texts/endnotes.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
        )
        spine = '<itemref idref="a"/><itemref idref="notes" linear="no"/>'
        package = opf(spine, manifest)
        build_epub(
            str(self.epub),
            {
                "OEBPS/chapter.xhtml": chapter,
                "OEBPS/texts/endnotes.xhtml": endnotes,
                "OEBPS/nav.xhtml": NAV,
            },
            package,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_nested_base_resolution_and_h2(self):
        result = extract_epub(str(self.epub))
        self.assertTrue(result.complete, result.errors)
        tags = [u.tag for u in result.body]
        self.assertEqual(tags, ["h1", "h2", "p"])  # h2 是合法正文单位
        p1 = next(u for u in result.body if u.node_id == "p1")
        self.assertEqual(p1.canonical_text, "正文嵌套收尾")
        self.assertEqual(
            p1.refs[0].target, "OEBPS/texts/endnotes.xhtml#e1"
        )
        # 独立重新解析核对坐标
        canonical, offsets, markers = independent_canonical(
            str(self.epub), "OEBPS/chapter.xhtml", "p1"
        )
        self.assertEqual(p1.canonical_text, canonical)
        self.assertEqual([r.insertion_index for r in p1.refs], offsets)
        self.assertEqual(markers[0]["base"].rstrip("/").endswith("OEBPS/texts/sub"), True)

    def test_note_chain_exports_both_without_cycle(self):
        result = extract_epub(str(self.epub))
        targets = [n.target for n in result.notes]
        self.assertEqual(
            targets,
            ["OEBPS/texts/endnotes.xhtml#e1", "OEBPS/texts/endnotes.xhtml#e2"],
        )
        e1, e2 = result.notes
        # e1 内部单位包含 h2 与 p
        self.assertEqual([u.tag for u in e1.units], ["h2", "p"])
        self.assertEqual(e1.refs[0].target, e2.target)
        self.assertIsNone(e1.refs[0].error)
        self.assertEqual(e2.units[0].canonical_text, "第二层，无环终止")
        # 链上每一层只导出一次
        self.assertEqual(len({n.target for n in result.notes}), 2)
        self.assertEqual(e2.note_order, e1.note_order + 1)


class PathResolutionTests(unittest.TestCase):
    def test_traversal_outside_archive_rejected(self):
        from epub_extract.extractor import ExtractionError, _join_base

        # OEBPS/texts/ 向上三级超出归档根，normpath 后仍带 ../
        with self.assertRaises(ExtractionError):
            _join_base("OEBPS/texts/", "../../../escape.xhtml")
        with self.assertRaises(ExtractionError):
            _join_base("OEBPS/a.xhtml", "/abs/path.xhtml")
        # 回到归档根仍在归档命名空间内，允许
        self.assertEqual(
            _join_base("OEBPS/texts/", "../../root-note.xhtml#e")[0],
            "root-note.xhtml",
        )
        # 纯 fragment 留在当前文档
        self.assertEqual(
            _join_base("OEBPS/a.xhtml", "#frag"),
            ("OEBPS/a.xhtml", "frag"),
        )


class OpfXmlBaseTests(unittest.TestCase):
    """OPF manifest 上的 xml:base 必须参与 item href 解析。"""

    def test_manifest_xml_base(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            epub = Path(td) / "opf-base.epub"
            package = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">x</dc:identifier><dc:title>t</dc:title><dc:language>zh</dc:language>
</metadata>
<manifest xml:base="texts/">
<item id="a" href="chapter-a.xhtml" media-type="application/xhtml+xml"/>
<item id="nav" href="../nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
</manifest>
<spine><itemref idref="a"/></spine>
</package>"""
            chapter = _doc('<body><p id="p">OPF base 正文</p></body>')
            build_epub(
                str(epub),
                {
                    "OEBPS/texts/chapter-a.xhtml": chapter,
                    "OEBPS/nav.xhtml": NAV,
                },
                package,
            )
            result = extract_epub(str(epub))
            self.assertTrue(result.complete)
            self.assertEqual(result.body[0].doc, "OEBPS/texts/chapter-a.xhtml")
            self.assertEqual(result.body[0].canonical_text, "OPF base 正文")


def _doc(body: str) -> str:
    return f"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>t</title></head>
{body}
</html>
"""


if __name__ == "__main__":
    unittest.main()
