"""在 tmp_path 下合成小型 EPUB3 夹具，用于补充预置 fixtures 未覆盖的场景。

所有夹具均为本地合成的纯文本 EPUB3，仅用于测试，不做 OCR/排版相关内容。
"""

from __future__ import annotations

import zipfile

MIMETYPE = "application/epub+zip"

CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
  <rootfiles>
    <rootfile full-path="OEBPS/package.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

NAV = """<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>nav</title></head>
<body><nav epub:type="toc"><ol>
<li><a href="chapter-a.xhtml">A</a></li>
<li><a href="chapter-b.xhtml">B</a></li>
</ol></nav></body></html>
"""


def build_epub(path: str, entries: dict[str, str], opf: str) -> None:
    """按 EPUB 要求把 mimetype 作为首个且不压缩的条目写入。"""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            zipfile.ZipInfo("mimetype"),
            MIMETYPE,
            compress_type=zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/container.xml", CONTAINER)
        zf.writestr("OEBPS/package.opf", opf)
        for name, content in entries.items():
            zf.writestr(name, content)


def opf(spine_xml: str, manifest_items: str) -> str:
    return f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">synthetic</dc:identifier>
<dc:title>合成测试书</dc:title>
<dc:language>zh</dc:language>
</metadata>
<manifest>
{manifest_items}
</manifest>
<spine>
{spine_xml}
</spine>
</package>
"""


XHTML_ITEMS = """<item id="a" href="chapter-a.xhtml" media-type="application/xhtml+xml"/>
<item id="b" href="chapter-b.xhtml" media-type="application/xhtml+xml"/>
<item id="notes" href="notes.xhtml" media-type="application/xhtml+xml"/>
<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>"""
