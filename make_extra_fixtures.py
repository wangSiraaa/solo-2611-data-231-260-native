"""生成补充测试用 PDF（本地合成，仅用于测试给定 fixtures 未覆盖的情形）。

fixtures/forms.pdf 与 fixtures/bad-cycle.pdf 是给定真实样例；本脚本生成的
PDF 只补充以下情形：checkbox 的 Off 与缺值、多选 Choice、V/I 不一致、合并
field+widget、无 widget 字段、不在任何页面 Annots 的 widget、/Sig 与
pushbutton（不支持类型）、/Ff 继承、断裂父链、归属冲突。

用法：python3 make_extra_fixtures.py [输出目录]   默认 output/
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    IndirectObject,
    NameObject,
    NumberObject,
    TextStringObject,
)

N = NameObject


def _add(writer: PdfWriter, obj: Any) -> IndirectObject:
    return writer._add_object(obj)


def _ap_stream(writer: PdfWriter) -> IndirectObject:
    s = DecodedStreamObject()
    s.set_data(b"q 0 0 1 rg 0 0 18 18 re f Q")
    s[N("/Type")] = N("/XObject")
    s[N("/Subtype")] = N("/Form")
    s[N("/BBox")] = ArrayObject([FloatObject(0), FloatObject(0), FloatObject(18), FloatObject(18)])
    return _add(writer, s)


def _widget(
    writer: PdfWriter,
    page_ref: Optional[IndirectObject],
    rect: Tuple[float, float, float, float],
    states: Optional[List[str]] = None,
    as_state: Optional[str] = None,
    parent: Optional[IndirectObject] = None,
) -> Tuple[IndirectObject, DictionaryObject]:
    w = DictionaryObject()
    w[N("/Type")] = N("/Annot")
    w[N("/Subtype")] = N("/Widget")
    w[N("/Rect")] = ArrayObject(FloatObject(v) for v in rect)
    w[N("/F")] = NumberObject(4)
    if page_ref is not None:
        w[N("/P")] = page_ref
    if parent is not None:
        w[N("/Parent")] = parent
    if states is not None:
        normal = DictionaryObject()
        for st in states:
            normal[N("/" + st)] = _ap_stream(writer)
        ap = DictionaryObject()
        ap[N("/N")] = normal
        w[N("/AP")] = ap
    if as_state is not None:
        w[N("/AS")] = N("/" + as_state)
    return _add(writer, w), w


def _field(writer: PdfWriter, t: str, ft: Optional[str] = None, **entries: Any) -> Tuple[IndirectObject, DictionaryObject]:
    f = DictionaryObject()
    f[N("/T")] = TextStringObject(t)
    if ft is not None:
        f[N("/FT")] = N(ft)
    for k, v in entries.items():
        f[N("/" + k)] = v
    return _add(writer, f), f


def _finish(writer: PdfWriter, pages: List[Any], annots: Dict[int, List[IndirectObject]],
            fields: List[IndirectObject], path: Path) -> Path:
    for i, annot_list in annots.items():
        if annot_list:
            pages[i][N("/Annots")] = ArrayObject(annot_list)
    acro = DictionaryObject()
    acro[N("/Fields")] = ArrayObject(fields)
    writer._root_object[N("/AcroForm")] = _add(writer, acro)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


def build_states_form(path: Path) -> Path:
    """Off/缺值、多选、V/I 不一致、合并 widget、无 widget、孤立 widget、不支持类型、Ff 继承。"""
    writer = PdfWriter()
    page1 = writer.add_blank_page(width=300, height=300)
    page2 = writer.add_blank_page(width=300, height=300)
    p1, p2 = page1.indirect_reference, page2.indirect_reference
    annots: Dict[int, List[IndirectObject]] = {0: [], 1: []}
    fields: List[IndirectObject] = []

    # 1) checkbox 显式 Off（Off 与缺值不同）
    ref, f = _field(writer, "cb_off", "/Btn", V=N("/Off"), Ff=NumberObject(0))
    wref, w = _widget(writer, p1, (10, 250, 30, 270), states=["Off", "Yes"], as_state="Off", parent=ref)
    f[N("/Kids")] = ArrayObject([wref])
    annots[0].append(wref)
    fields.append(ref)

    # 2) checkbox 无 V（missing，不等于 Off）
    ref, f = _field(writer, "cb_unset", "/Btn", Ff=NumberObject(0))
    wref, w = _widget(writer, p1, (40, 250, 60, 270), states=["Off", "Yes"], as_state="Off", parent=ref)
    f[N("/Kids")] = ArrayObject([wref])
    annots[0].append(wref)
    fields.append(ref)

    # 3) 多选 Choice：V 数组 + /I
    ref, f = _field(
        writer, "multi", "/Ch",
        Ff=NumberObject(1 << 21),
        Opt=ArrayObject([
            ArrayObject([TextStringObject("a"), TextStringObject("甲")]),
            ArrayObject([TextStringObject("b"), TextStringObject("乙")]),
            ArrayObject([TextStringObject("c"), TextStringObject("丙")]),
        ]),
        V=ArrayObject([TextStringObject("a"), TextStringObject("c")]),
        I=ArrayObject([NumberObject(0), NumberObject(2)]),
    )
    wref, w = _widget(writer, p2, (10, 200, 120, 220), parent=ref)
    f[N("/Kids")] = ArrayObject([wref])
    annots[1].append(wref)
    fields.append(ref)

    # 4) V/I 不一致：V=Y 但 I=[0]（指向 X）——保留 V，记诊断
    ref, f = _field(
        writer, "vi_bad", "/Ch",
        Ff=NumberObject(0),
        Opt=ArrayObject([
            ArrayObject([TextStringObject("X"), TextStringObject("ex")]),
            ArrayObject([TextStringObject("Y"), TextStringObject("why")]),
        ]),
        V=TextStringObject("Y"),
        I=ArrayObject([NumberObject(0)]),
    )
    wref, w = _widget(writer, p2, (10, 170, 120, 190), parent=ref)
    f[N("/Kids")] = ArrayObject([wref])
    annots[1].append(wref)
    fields.append(ref)

    # 5) 合并 field+widget（字段字典自身即 widget）
    ref, f = _field(writer, "merged", "/Tx", V=TextStringObject("合并部件值"))
    f[N("/Type")] = N("/Annot")
    f[N("/Subtype")] = N("/Widget")
    f[N("/Rect")] = ArrayObject([FloatObject(10), FloatObject(130), FloatObject(120), FloatObject(150)])
    f[N("/F")] = NumberObject(4)
    f[N("/P")] = p1
    annots[0].append(ref)
    fields.append(ref)

    # 6) 无 widget 的字段
    ref, f = _field(writer, "ghost", "/Tx", V=TextStringObject("隐写"))
    fields.append(ref)

    # 7) widget 不在任何页面 Annots 中
    ref, f = _field(writer, "orphan", "/Tx", V=TextStringObject("孤立"))
    wref, w = _widget(writer, p1, (10, 100, 120, 120), parent=ref)
    f[N("/Kids")] = ArrayObject([wref])
    fields.append(ref)  # 注意：wref 不加入任何 annots

    # 8) /Sig 不支持
    ref, f = _field(writer, "sig", "/Sig")
    fields.append(ref)

    # 9) pushbutton 不支持
    ref, f = _field(writer, "push", "/Btn", Ff=NumberObject(1 << 16))
    wref, w = _widget(writer, p1, (10, 70, 60, 90), parent=ref)
    f[N("/Kids")] = ArrayObject([wref])
    annots[0].append(wref)
    fields.append(ref)

    # 10) /Ff 继承：父 /Tx + Ff=1(ReadOnly)，子终端不带 Ff
    pref, pf = _field(writer, "ro_group", "/Tx", Ff=NumberObject(1))
    cref, cf = _field(writer, "ro_text", V=TextStringObject("只读继承"))
    cf[N("/Parent")] = pref
    wref, w = _widget(writer, p2, (10, 40, 120, 60), parent=cref)
    cf[N("/Kids")] = ArrayObject([wref])
    pf[N("/Kids")] = ArrayObject([cref])
    annots[1].append(wref)
    fields.append(pref)

    return _finish(writer, [page1, page2], annots, fields, path)


def build_broken_parent(path: Path) -> Path:
    """父链断裂：/Parent 指向错误对象；/Parent 为悬空引用。"""
    writer = PdfWriter()
    page1 = writer.add_blank_page(width=300, height=300)
    p1 = page1.indirect_reference
    annots: Dict[int, List[IndirectObject]] = {0: []}
    fields: List[IndirectObject] = []

    # root_b：合法根（合并 widget），会被 root_a 的孩子错误指认为 /Parent
    ref_b, fb = _field(writer, "root_b", "/Tx", V=TextStringObject("b"))
    fb[N("/Type")] = N("/Annot")
    fb[N("/Subtype")] = N("/Widget")
    fb[N("/Rect")] = ArrayObject([FloatObject(10), FloatObject(250), FloatObject(120), FloatObject(270)])
    fb[N("/F")] = NumberObject(4)
    fb[N("/P")] = p1
    annots[0].append(ref_b)

    # root_a -> kid；kid 的 /Parent 错指 root_b；kid 的 widget /Parent 悬空
    ref_a, fa = _field(writer, "root_a", "/Tx")
    ref_k, fk = _field(writer, "kid", V=TextStringObject("k"))
    fk[N("/Parent")] = ref_b  # 父链断裂：实际从 root_a 到达
    wref, w = _widget(writer, p1, (10, 220, 120, 240), parent=None)
    w[N("/Parent")] = IndirectObject(999, 0, writer)  # 悬空引用
    w[N("/P")] = p1
    fk[N("/Kids")] = ArrayObject([wref])
    fa[N("/Kids")] = ArrayObject([ref_k])
    annots[0].append(wref)
    fields.extend([ref_a, ref_b])

    return _finish(writer, [page1], annots, fields, path)


def build_conflicting(path: Path) -> Path:
    """归属冲突：两个根字段的 /Kids 引用同一个终端字段。"""
    writer = PdfWriter()
    page1 = writer.add_blank_page(width=300, height=300)
    p1 = page1.indirect_reference
    annots: Dict[int, List[IndirectObject]] = {0: []}

    ref_s, fs = _field(writer, "shared", "/Tx", V=TextStringObject("共享字段"))
    wref, w = _widget(writer, p1, (10, 250, 120, 270), parent=ref_s)
    fs[N("/Kids")] = ArrayObject([wref])
    annots[0].append(wref)

    ref_r1, fr1 = _field(writer, "root1")
    fr1[N("/Kids")] = ArrayObject([ref_s])
    fs[N("/Parent")] = ref_r1
    ref_r2, fr2 = _field(writer, "root2")
    fr2[N("/Kids")] = ArrayObject([ref_s])  # 同一对象，第二个“父亲”

    return _finish(writer, [page1], annots, [ref_r1, ref_r2], path)


def build_all(out_dir: Path) -> Dict[str, Path]:
    out = {
        "states_form": build_states_form(out_dir / "states_form.pdf"),
        "broken_parent": build_broken_parent(out_dir / "broken_parent.pdf"),
        "conflicting": build_conflicting(out_dir / "conflicting.pdf"),
    }
    return out


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output")
    for name, p in build_all(target).items():
        print(f"{name}: {p}")
