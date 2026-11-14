"""AcroForm 填写值采集：extract / inspect。

读取普通 AcroForm PDF（如 fixtures/forms.pdf），把逻辑字段值采集为数据记录，
并输出每个终端字段的页面 widget 证据。只读，不做 OCR / 渲染 / 表单编辑 /
JavaScript 执行；不支持 XFA、签名验证、脚本计算或通用 PDF 修复。

设计要点
--------
* 遍历 AcroForm 的 /Fields -> /Kids -> /Parent 树（不是按页面 Annots 顺序拼字段），
  非终端字段的部分名 /T 组成完整逻辑名，名称分段与每一级对象引用都保留。
* 同一终端字段的多个 widget（可跨页）只产生一条填写记录。
* /FT、/Ff、/V、/DV 按 ISO 32000 可继承规则沿父链取得（仅这四项，/Opt 不继承）。
* 字段环（Kids/Parent 成环）明确抛出 AcroFormStructureError，绝不无限递归；
  断裂父链、归属冲突等记入 diagnostics，不中断其余字段。
* 字段 /V 是逻辑当前值；widget 的 /AS、/AP 只是外观证据，两者分别报告。
  checkbox 的 V=Yes 而 widget AS=Off 时保留逻辑值并标 appearance_mismatch，
  不用视觉状态覆盖字段值；无可靠外观证据时标 unknown，不猜测。
* 缺少 /V 时 status=missing，绝不用 /DV 默认值冒充已填写。
* inspect() 用独立的对象遍历（字段树 + 页面 Annots + 全 xref 扫描）核对字段
  数量与页面引用，不是把 get_fields() 的结果原样输出。

命令行：
    python3 acroform_extract.py extract fixtures/forms.pdf
    python3 acroform_extract.py inspect fixtures/forms.pdf
"""

from __future__ import annotations

import dataclasses
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Tuple, Union

from pypdf import PdfReader
from pypdf.generic import (
    ArrayObject,
    BooleanObject,
    ByteStringObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NullObject,
    NumberObject,
    TextStringObject,
)

__all__ = [
    "AcroFormError",
    "AcroFormStructureError",
    "AcroFormExtractor",
    "WidgetEvidence",
    "FieldRecord",
    "ExtractionResult",
    "extract",
    "inspect",
]

# ---------------------------------------------------------------------------
# 常量：/Ff 标志位（ISO 32000 表格中 1-based 位号 -> 掩码）
# ---------------------------------------------------------------------------
FF_READ_ONLY = 1 << 0            # bit 1，所有字段
FF_REQUIRED = 1 << 1             # bit 2
FF_NO_EXPORT = 1 << 2            # bit 3
FF_TX_MULTILINE = 1 << 12        # bit 13，文本
FF_TX_PASSWORD = 1 << 13         # bit 14，文本
FF_BTN_NO_TOGGLE_TO_OFF = 1 << 14  # bit 15，按钮
FF_BTN_RADIO = 1 << 15           # bit 16，按钮（radio 组）
FF_BTN_PUSHBUTTON = 1 << 16      # bit 17，按钮（pushbutton，不支持）
FF_CH_COMBO = 1 << 17            # bit 18，choice
FF_CH_EDIT = 1 << 18             # bit 19，choice
FF_CH_SORT = 1 << 19             # bit 20，choice
FF_CH_MULTI_SELECT = 1 << 21     # bit 22，choice
FF_BTN_RADIOS_IN_UNISON = 1 << 25  # bit 26，按钮

# 可继承字段属性（ISO 32000：仅 FT/Ff/V/DV 沿父链继承；/Opt 不继承）
_INHERITABLE = ("/FT", "/Ff", "/V", "/DV")

# 遍历保护：字段树深度硬上限（循环由路径集合检测，此为兜底）
_MAX_DEPTH = 100

_ROOT_KEY = ("ROOT",)
_ROOT_DISP = "/Fields"


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class AcroFormError(Exception):
    """AcroForm 采集失败（含结构性硬失败）。"""

    def __init__(self, message: str, diagnostics: Optional[List[str]] = None):
        super().__init__(message)
        self.diagnostics: List[str] = list(diagnostics or [message])


class AcroFormStructureError(AcroFormError):
    """字段树结构损坏（如字段环）。明确失败，不产出部分结果。"""


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class WidgetEvidence:
    """一个页面 widget 控件的证据（外观只作证据，不作逻辑值）。"""

    object_ref: str                       # 如 "9 0 R"；直接对象为 "<direct>"
    page: Optional[int]                   # 1-based 页号；不在任何页面 Annots 中则为 None
    rect: Optional[List[float]]           # /Rect [x1 y1 x2 y2]
    appearance_state: Optional[str]       # /AS（去掉前导 "/"），缺失为 None
    appearance_states: Optional[List[str]]  # /AP/N 可用状态集合；无 AP 或单流外观为 None
    on_state: Optional[str] = None        # 按钮类 widget 的导出状态名（非 Off 的那个）
    evidence: str = "unknown"             # consistent | stale | unknown | not_applicable
    diagnostics: List[str] = field(default_factory=list)


@dataclass
class FieldRecord:
    """一个终端字段的填写记录（同一终端字段多 widget 只产生一条）。"""

    name: str                             # 完整逻辑名，如 "contact.name"
    name_segments: List[str]              # 各级 /T 部分名
    node_refs: List[str]                  # 从根到终端每一级对象引用（对象身份）
    terminal_ref: str                     # 终端字段对象引用
    field_type: str                       # text | choice | checkbox | radio | unsupported | unknown
    flags: int                            # 继承解析后的 /Ff
    sources: Dict[str, str]               # ft/ff/v/dv 各项来源："self" | "inherited:<ref>" | "missing"
    status: str                           # filled | missing | unsupported | unknown_type
    value: Any = None                     # 逻辑当前值（/V）；missing 为 None
    default_value: Any = None             # /DV 默认值（仅记录，绝不冒充已填写）
    # Choice 专有
    options: List[Dict[str, str]] = field(default_factory=list)   # [{export, display}]
    value_labels: Optional[List[str]] = None                      # 当前值对应的显示标签
    indices: Optional[List[int]] = None                           # /I
    combo: bool = False
    multi_select: bool = False
    # 按钮类专有
    export_states: List[str] = field(default_factory=list)        # 各 widget 导出状态名并集
    # widget 证据
    widgets: List[WidgetEvidence] = field(default_factory=list)
    appearance: str = "not_applicable"    # consistent | stale | unknown | not_applicable
    appearance_mismatch: bool = False
    diagnostics: List[str] = field(default_factory=list)


@dataclass
class ExtractionResult:
    """extract() 的产出：字段数据 + 页面控件证据。"""

    source: str
    page_count: int
    fields: List[FieldRecord]
    diagnostics: List[str] = field(default_factory=list)

    @property
    def field_count(self) -> int:
        return len(self.fields)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "page_count": self.page_count,
            "field_count": self.field_count,
            "diagnostics": list(self.diagnostics),
            "fields": [dataclasses.asdict(f) for f in self.fields],
        }

    def to_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("ensure_ascii", False)
        kwargs.setdefault("indent", 2)
        return json.dumps(self.to_dict(), **kwargs)


# ---------------------------------------------------------------------------
# 低层工具
# ---------------------------------------------------------------------------
def _raw(d: DictionaryObject, key: str) -> Any:
    """读取字典项的原始值（不解引用）。

    pypdf 6 的 __getitem__ 会自动解引用，为保留对象身份必须用 raw_get；
    pypdf 5 无自动解引用，get 即原始值。
    """
    raw_get = getattr(d, "raw_get", None)
    if raw_get is not None:
        try:
            return raw_get(key)
        except KeyError:
            return None
    return d.get(key)


def _resolve(obj: Any) -> Any:
    """解引用间接对象；直接对象原样返回。悬空引用会抛异常（调用方捕获）。"""
    if isinstance(obj, IndirectObject):
        return obj.get_object()
    return obj


def _key_of(obj: Any) -> Tuple[Any, ...]:
    """对象身份：间接对象用 (idnum, generation)，直接对象用 Python 身份。"""
    if isinstance(obj, IndirectObject):
        return ("ref", obj.idnum, obj.generation)
    return ("direct", id(obj))


def _fmt_ref(obj: Any) -> str:
    if isinstance(obj, IndirectObject):
        return f"{obj.idnum} {obj.generation} R"
    return "<direct>"


def _fmt_key(key: Tuple[Any, ...]) -> str:
    """把 _key_of 的身份元组格式化回展示用引用。"""
    if key and key[0] == "ref":
        return f"{key[1]} {key[2]} R"
    return "<direct>"


def _is_stream(obj: Any) -> bool:
    return hasattr(obj, "get_data")


def _name_str(obj: Any) -> str:
    """NameObject '/Yes' -> 'Yes'；其他字符串原样。"""
    s = str(obj)
    return s[1:] if s.startswith("/") else s


def _text_str(obj: Any) -> Tuple[Optional[str], Optional[str]]:
    """把 /V、/DV、/T 一类的值转成真实 Unicode 字符串。

    返回 (值, 诊断)；无法合理转换时值为 None。
    """
    if isinstance(obj, TextStringObject):
        return str(obj), None
    if isinstance(obj, NameObject):
        return str(obj), f"name_object_used_as_text:{obj}"
    if isinstance(obj, ByteStringObject):
        data = bytes(obj)
        for enc in ("utf-16", "utf-8", "latin-1"):
            try:
                return data.decode(enc), f"byte_string_decoded_as:{enc}"
            except (UnicodeDecodeError, ValueError):
                continue
        return data.decode("latin-1", errors="replace"), "byte_string_decoded_with_replacement"
    if isinstance(obj, str):
        return str(obj), None
    return None, f"unexpected_text_type:{type(obj).__name__}"


def _open_reader(source: Union[str, Path, BinaryIO]) -> Tuple[PdfReader, str]:
    reader = PdfReader(source)
    if reader.is_encrypted:
        raise AcroFormError("encrypted PDF not supported")
    name = getattr(source, "name", None) or str(source)
    return reader, name


def _acroform_fields(reader: PdfReader, notes: Optional[List[str]] = None) -> ArrayObject:
    try:
        root = reader.trailer["/Root"]
    except KeyError as exc:
        raise AcroFormError("not a valid PDF: no /Root") from exc
    acro = _resolve(_raw(root, "/AcroForm"))
    if acro is None:
        raise AcroFormError("no /AcroForm in document catalog")
    if not isinstance(acro, DictionaryObject):
        raise AcroFormError("/AcroForm is not a dictionary")
    if _raw(acro, "/XFA") is not None:
        raise AcroFormError("XFA forms are not supported (only plain AcroForm)")
    fields = _resolve(_raw(acro, "/Fields"))
    if fields is None:
        raise AcroFormError("/AcroForm has no /Fields array")
    if not isinstance(fields, ArrayObject):
        raise AcroFormError("/AcroForm /Fields is not an array")
    na = _resolve_safe(_raw(acro, "/NeedAppearances"))
    # pypdf 的 BooleanObject 不是 bool 子类，真值在 .value 上
    if isinstance(na, BooleanObject) and bool(getattr(na, "value", False)) and notes is not None:
        # 仅提示：查看器可能重生成外观，AS/AP 证据可信度需留意
        notes.append("need_appearances_true: viewer may regenerate appearances")
    return fields


# ---------------------------------------------------------------------------
# 页面 Annots 扫描（widget -> 页号 的独立证据来源）
# ---------------------------------------------------------------------------
@dataclass
class _AnnotsScan:
    widget_pages: Dict[Tuple[Any, ...], List[int]]      # widget key -> 1-based 页号列表
    page_by_refkey: Dict[Tuple[Any, ...], int]          # 页对象 ref key -> 页号
    page_by_objid: Dict[int, int]                       # id(页对象) -> 页号
    widget_refs: Dict[Tuple[Any, ...], str]             # widget key -> 展示用引用
    pages: List[Any] = field(default_factory=list)      # 页对象本身（直接 /P 字典做内容比较）


def _scan_annots(reader: PdfReader) -> _AnnotsScan:
    scan = _AnnotsScan({}, {}, {}, {}, [])
    for index, page in enumerate(reader.pages):
        page_no = index + 1
        scan.pages.append(page)
        scan.page_by_objid[id(page)] = page_no
        pref = getattr(page, "indirect_reference", None)
        if isinstance(pref, IndirectObject):
            scan.page_by_refkey[_key_of(pref)] = page_no
        annots = _resolve(_raw(page, "/Annots"))
        if annots is None:
            continue
        if not isinstance(annots, ArrayObject):
            continue
        for entry in annots:
            try:
                annot = _resolve(entry)
            except Exception:
                continue
            if not isinstance(annot, DictionaryObject) or _is_stream(annot):
                continue
            if str(_raw(annot, "/Subtype")) != "/Widget":
                continue
            key = _key_of(entry)
            scan.widget_pages.setdefault(key, []).append(page_no)
            scan.widget_refs.setdefault(key, _fmt_ref(entry))
    return scan


def _locate_page(scan: _AnnotsScan, raw_p: Any) -> Optional[int]:
    """把 widget 的 /P（间接引用或直接字典）定位到页号。"""
    if raw_p is None:
        return None
    if isinstance(raw_p, IndirectObject):
        hit = scan.page_by_refkey.get(_key_of(raw_p))
        if hit is not None:
            return hit
    try:
        target = _resolve(raw_p)
    except Exception:
        return None
    hit = scan.page_by_objid.get(id(target))
    if hit is not None:
        return hit
    # 直接字典 /P（文件内嵌副本）：按内容相等匹配页面
    if isinstance(target, DictionaryObject):
        for page_no, page in enumerate(scan.pages, start=1):
            try:
                if target == page:
                    return page_no
            except Exception:
                continue
    return None


# ---------------------------------------------------------------------------
# 字段树遍历
# ---------------------------------------------------------------------------
class _TreeWalker:
    """沿 /Fields-/Kids 自顶向下遍历，产出终端字段记录。

    循环 -> 抛 AcroFormStructureError；归属冲突/断裂引用 -> diagnostics。
    """

    def __init__(self, reader: PdfReader):
        self.reader = reader
        self.scan = _scan_annots(reader)
        self.diagnostics: List[str] = []
        self.records: List[FieldRecord] = []
        self.owner: Dict[Tuple[Any, ...], Tuple[Any, ...]] = {}
        self.refnames: Dict[Tuple[Any, ...], str] = {_ROOT_KEY: _ROOT_DISP}
        self.full_names: Dict[str, str] = {}

    # -- 主入口 ---------------------------------------------------------
    def walk(self, fields: ArrayObject) -> None:
        for entry in fields:
            self._walk_node(
                entry,
                parent_key=_ROOT_KEY,
                parent_node=None,
                segments=[],
                inherited={},
                chain=[],
            )

    # -- 节点 -----------------------------------------------------------
    def _walk_node(
        self,
        ref: Any,
        parent_key: Tuple[Any, ...],
        parent_node: Optional[DictionaryObject],
        segments: List[str],
        inherited: Dict[str, Tuple[Any, str]],
        chain: List[Tuple[Tuple[Any, ...], str, Any]],
    ) -> None:
        key = _key_of(ref)
        disp = _fmt_ref(ref)
        self.refnames.setdefault(key, disp)

        # 1) 字段环：硬失败（先于归属判断，保证环一定被报成环）
        for ckey, cdisp, _cnode in chain:
            if ckey == key:
                path = " -> ".join([c[1] for c in chain] + [disp])
                raise AcroFormStructureError(
                    f"cycle in field tree: {path}",
                    diagnostics=[f"cycle_detected: {path}"],
                )
        if len(chain) >= _MAX_DEPTH:
            raise AcroFormStructureError(
                f"field tree deeper than {_MAX_DEPTH} levels",
                diagnostics=[f"tree_too_deep: at {disp}"],
            )

        # 2) 归属冲突 / 重复引用：诊断并跳过（不重复产出记录）
        if key in self.owner:
            prev = self.owner[key]
            if prev != parent_key:
                self.diagnostics.append(
                    "conflicting_ownership: "
                    f"{disp} reached from {self.refnames.get(parent_key, '?')} "
                    f"but already owned by {self.refnames.get(prev, '?')}"
                )
            else:
                self.diagnostics.append(
                    f"duplicate_kid: {disp} listed twice under "
                    f"{self.refnames.get(parent_key, '?')}"
                )
            return
        self.owner[key] = parent_key

        # 3) 解析节点
        try:
            node = _resolve(ref)
        except Exception as exc:
            self.diagnostics.append(f"broken_ref: {disp} cannot be resolved ({exc})")
            return
        if not isinstance(node, DictionaryObject) or _is_stream(node):
            self.diagnostics.append(
                f"broken_ref: {disp} is not a field dictionary "
                f"({type(node).__name__})"
            )
            return

        # 4) 父链一致性（断裂父链 -> 诊断，不中断）
        self._check_parent(node, parent_key, parent_node, disp)

        # 5) 名称分段与继承属性
        t_raw = _resolve(_raw(node, "/T"))
        t: Optional[str] = None
        if t_raw is not None:
            t, tdiag = _text_str(t_raw)
            if tdiag:
                self.diagnostics.append(f"odd_partial_name: {disp}: {tdiag}")
        segs = segments + ([t] if t else [])
        inh = dict(inherited)
        for attr in _INHERITABLE:
            val = _raw(node, attr)
            if val is not None and not isinstance(_resolve_safe(val), NullObject):
                inh[attr] = (val, disp)

        # 6) Kids 分类：纯 widget 还是子字段
        is_widget = str(_raw(node, "/Subtype")) == "/Widget"
        widget_kids: List[Tuple[Any, DictionaryObject]] = []
        field_kids: List[Any] = []
        kids_raw = _raw(node, "/Kids")
        if kids_raw is not None:
            try:
                kids_arr = _resolve(kids_raw)
            except Exception as exc:
                self.diagnostics.append(f"broken_kids: {disp} /Kids unresolvable ({exc})")
                kids_arr = None
            if kids_arr is not None and not isinstance(kids_arr, ArrayObject):
                self.diagnostics.append(f"broken_kids: {disp} /Kids is not an array")
                kids_arr = None
            for kref in kids_arr or []:
                try:
                    kd = _resolve(kref)
                except Exception as exc:
                    self.diagnostics.append(
                        f"broken_kid_ref: {_fmt_ref(kref)} under {disp} ({exc})"
                    )
                    continue
                if (
                    isinstance(kd, DictionaryObject)
                    and not _is_stream(kd)
                    and str(_raw(kd, "/Subtype")) == "/Widget"
                    and _raw(kd, "/T") is None
                    and _raw(kd, "/Kids") is None
                ):
                    widget_kids.append((kref, kd))
                else:
                    field_kids.append(kref)
        if widget_kids and field_kids:
            self.diagnostics.append(
                f"mixed_kids: {disp} has both widget and subfield kids"
            )

        # 7) 终端判定：合并 widget 字段，或没有子字段
        terminal = is_widget or not field_kids
        if terminal:
            widgets: List[Tuple[Any, DictionaryObject]] = []
            if is_widget:
                widgets.append((ref, node))
            widgets.extend(widget_kids)
            self._make_record(ref, disp, key, node, segs, inh, widgets, chain)

        # 8) 下潜子字段
        for kref in field_kids:
            self._walk_node(
                kref,
                parent_key=key,
                parent_node=node,
                segments=segs,
                inherited=inh,
                chain=chain + [(key, disp, node)],
            )

    # -- 父链检查 -------------------------------------------------------
    def _check_parent(
        self,
        node: DictionaryObject,
        parent_key: Tuple[Any, ...],
        parent_node: Optional[DictionaryObject],
        disp: str,
    ) -> None:
        raw_parent = _raw(node, "/Parent")
        if parent_key is _ROOT_KEY:
            if raw_parent is not None:
                self.diagnostics.append(
                    f"broken_parent_chain: root field {disp} unexpectedly "
                    f"has /Parent {_fmt_ref(raw_parent)}"
                )
            return
        if raw_parent is None:
            self.diagnostics.append(
                f"missing_parent_ref: {disp} has no /Parent "
                f"(expected {self.refnames.get(parent_key, '?')})"
            )
            return
        if isinstance(raw_parent, IndirectObject):
            if _key_of(raw_parent) == parent_key:
                return
            resolved_parent: Any = None
            try:
                resolved_parent = _resolve(raw_parent)
            except Exception as exc:
                self.diagnostics.append(
                    f"broken_parent_ref: {disp} /Parent "
                    f"{_fmt_ref(raw_parent)} unresolvable ({exc})"
                )
                return
            if resolved_parent is None:
                # pypdf 对悬空引用可能返回 None 而非抛异常
                self.diagnostics.append(
                    f"broken_parent_ref: {disp} /Parent "
                    f"{_fmt_ref(raw_parent)} resolves to nothing"
                )
                return
            self.diagnostics.append(
                f"parent_mismatch: {disp} /Parent is {_fmt_ref(raw_parent)} "
                f"but reached via {self.refnames.get(parent_key, '?')}"
            )
            return
        # 直接字典 /Parent：按身份/内容比较
        if parent_node is not None and (raw_parent is parent_node or raw_parent == parent_node):
            return
        self.diagnostics.append(
            f"parent_mismatch: {disp} has direct /Parent not matching "
            f"{self.refnames.get(parent_key, '?')}"
        )

    # -- 终端记录 -------------------------------------------------------
    def _make_record(
        self,
        ref: Any,
        disp: str,
        key: Tuple[Any, ...],
        node: DictionaryObject,
        segs: List[str],
        inh: Dict[str, Tuple[Any, str]],
        widgets: List[Tuple[Any, DictionaryObject]],
        chain: List[Tuple[Tuple[Any, ...], str, Any]],
    ) -> None:
        diags: List[str] = []

        def pick(attr: str) -> Tuple[Any, Optional[str]]:
            pair = inh.get(attr)
            return (pair[0], pair[1]) if pair else (None, None)

        ft_raw, ft_src = pick("/FT")
        ff_raw, ff_src = pick("/Ff")
        v_raw, v_src = pick("/V")
        dv_raw, dv_src = pick("/DV")

        def source_of(src: Optional[str]) -> str:
            if src is None:
                return "missing"
            return "self" if src == disp else f"inherited:{src}"

        sources = {
            "ft": source_of(ft_src),
            "ff": source_of(ff_src),
            "v": source_of(v_src),
            "dv": source_of(dv_src),
        }

        flags = 0
        if ff_raw is not None:
            ff_val = _resolve_safe(ff_raw)
            if isinstance(ff_val, (NumberObject, int)) and not isinstance(ff_val, bool):
                flags = int(ff_val)
            else:
                diags.append(f"odd_flags: /Ff={ff_val!r} treated as 0")

        if segs:
            name = ".".join(segs)
        else:
            name = f"<unnamed:{disp}>"
            diags.append("unnamed_field: no /T on the whole parent chain")
        if name in self.full_names:
            diags.append(
                f"duplicate_field_name: '{name}' also used by {self.full_names[name]}"
            )
        else:
            self.full_names[name] = disp

        node_refs = [c[1] for c in chain] + [disp]
        ftype, type_diags = _classify(ft_raw, flags)
        diags.extend(type_diags)

        status = "missing"
        value: Any = None
        default_value: Any = None
        options: List[Dict[str, str]] = []
        value_labels: Optional[List[str]] = None
        indices: Optional[List[int]] = None
        combo = False
        multi = False
        export_states: List[str] = []

        if ftype == "text":
            status, value, d = _text_value(v_raw)
            diags.extend(d)
            default_value, d = _text_default(dv_raw)
            diags.extend(d)
        elif ftype == "choice":
            combo = bool(flags & FF_CH_COMBO)
            multi = bool(flags & FF_CH_MULTI_SELECT)
            (status, value, default_value, options, value_labels, indices, d) = (
                self._choice_value(node, v_raw, dv_raw, multi)
            )
            diags.extend(d)
        elif ftype in ("checkbox", "radio"):
            status, value, d = _btn_value(v_raw)
            diags.extend(d)
            default_value, d = _btn_default(dv_raw)
            diags.extend(d)
        elif ftype == "unknown":
            status = "unknown_type"
        else:  # unsupported
            status = "unsupported"

        # widget 证据
        btn_value = value if ftype in ("checkbox", "radio") else None
        evidences = [
            self._widget_evidence(wref, wdict, key, ftype, btn_value)
            for wref, wdict in widgets
        ]
        if not evidences:
            diags.append("no_widgets: terminal field has no widget annotation")

        if ftype in ("checkbox", "radio"):
            on_states: List[str] = []
            for ev in evidences:
                if ev.on_state is not None:
                    if ev.on_state in on_states and ftype == "radio" and not (
                        flags & FF_BTN_RADIOS_IN_UNISON
                    ):
                        diags.append(
                            f"duplicate_on_state: '{ev.on_state}' on multiple widgets"
                        )
                    on_states.append(ev.on_state)
            export_states = sorted(set(on_states))
            if status == "filled" and value != "Off" and value not in on_states:
                diags.append(
                    f"value_not_in_widget_states: /V '{value}' matches no widget "
                    f"on-state {on_states}"
                )

        mismatch = any(ev.evidence == "stale" for ev in evidences)
        appearance = _summarize_appearance(evidences, ftype)

        self.records.append(
            FieldRecord(
                name=name,
                name_segments=list(segs),
                node_refs=node_refs,
                terminal_ref=disp,
                field_type=ftype,
                flags=flags,
                sources=sources,
                status=status,
                value=value,
                default_value=default_value,
                options=options,
                value_labels=value_labels,
                indices=indices,
                combo=combo,
                multi_select=multi,
                export_states=export_states,
                widgets=evidences,
                appearance=appearance,
                appearance_mismatch=mismatch,
                diagnostics=diags,
            )
        )

    # -- Choice 值 ------------------------------------------------------
    def _choice_value(
        self,
        node: DictionaryObject,
        v_raw: Any,
        dv_raw: Any,
        multi: bool,
    ) -> Tuple[str, Any, Any, List[Dict[str, str]], Optional[List[str]], Optional[List[int]], List[str]]:
        diags: List[str] = []
        options: List[Dict[str, str]] = []

        # /Opt 不继承，只取终端自身
        opt_raw = _resolve_safe(_raw(node, "/Opt"))
        if opt_raw is None:
            diags.append("missing_opt: choice field has no /Opt")
        elif not isinstance(opt_raw, ArrayObject):
            diags.append("bad_opt: /Opt is not an array")
        else:
            for entry in opt_raw:
                entry = _resolve_safe(entry)
                if isinstance(entry, ArrayObject) and len(entry) >= 1:
                    ex, d1 = _text_str(_resolve_safe(entry[0]))
                    if len(entry) >= 2:
                        disp, d2 = _text_str(_resolve_safe(entry[1]))
                    else:
                        disp, d2 = ex, None
                    if ex is None:
                        diags.append("bad_opt_entry: export not a text string")
                        continue
                    for d in (d1, d2):
                        if d:
                            diags.append(f"odd_opt_entry: {d}")
                    options.append({"export": ex, "display": disp if disp is not None else ex})
                elif isinstance(entry, (TextStringObject, NameObject, ByteStringObject)):
                    s, d = _text_str(entry)
                    if s is None:
                        diags.append("bad_opt_entry: not a text string")
                        continue
                    if d:
                        diags.append(f"odd_opt_entry: {d}")
                    options.append({"export": s, "display": s})
                else:
                    diags.append(
                        f"bad_opt_entry: unexpected {type(entry).__name__}"
                    )
        exports = [o["export"] for o in options]

        def norm_values(raw: Any, what: str) -> Tuple[Optional[List[str]], List[str]]:
            ds: List[str] = []
            if raw is None:
                return None, ds
            val = _resolve_safe(raw)
            if isinstance(val, NullObject):
                return None, [f"null_{what}"]
            if isinstance(val, ArrayObject):
                out: List[str] = []
                for e in val:
                    s, d = _text_str(_resolve_safe(e))
                    if s is None:
                        ds.append(f"bad_{what}_entry: {type(e).__name__}")
                    else:
                        if d:
                            ds.append(f"odd_{what}_entry: {d}")
                        out.append(s)
                return out, ds
            s, d = _text_str(val)
            if s is None:
                ds.append(f"bad_{what}: {type(val).__name__}")
                return None, ds
            if d:
                ds.append(f"odd_{what}: {d}")
            return [s], ds

        values, d = norm_values(v_raw, "V")
        diags.extend(d)
        dv_values, d = norm_values(dv_raw, "DV")
        diags.extend(d)

        # /I 索引（不多继承，终端自身）
        indices: Optional[List[int]] = None
        i_raw = _resolve_safe(_raw(node, "/I"))
        if i_raw is not None:
            if isinstance(i_raw, ArrayObject):
                indices = []
                for e in i_raw:
                    e = _resolve_safe(e)
                    if isinstance(e, (NumberObject, int)):
                        indices.append(int(e))
                    else:
                        diags.append(f"bad_I_entry: {type(e).__name__}")
            else:
                diags.append("bad_I: /I is not an array")

        # 一致性检查：V/I 与 Opt
        if values is not None:
            for v in values:
                if v not in exports:
                    diags.append(f"value_not_in_opt: /V '{v}' not in Opt exports {exports}")
        if indices is not None:
            for i in indices:
                if not (0 <= i < len(options)):
                    diags.append(
                        f"index_out_of_range: /I {i} but Opt has {len(options)} entries"
                    )
        if values is not None and indices is not None and options:
            valid = [i for i in indices if 0 <= i < len(options)]
            from_i = sorted(exports[i] for i in valid)
            if sorted(values) != from_i:
                diags.append(
                    f"v_i_mismatch: /V {values} disagrees with /I {indices} "
                    f"(-> {sorted(exports[i] for i in valid)})"
                )

        if values is None:
            status = "missing"
            value = None
            value_labels = None
        else:
            status = "filled"
            if not multi and len(values) > 1:
                diags.append("multi_value_single_select: multiple /V but MultiSelect not set")
            value = values if multi else (values[0] if values else None)
            value_labels = [
                options[exports.index(v)]["display"] if v in exports else None
                for v in values
            ]

        default_value: Any = None
        if dv_values is not None:
            default_value = dv_values if multi else (dv_values[0] if dv_values else None)
        return status, value, default_value, options, value_labels, indices, diags

    # -- widget 证据 ----------------------------------------------------
    def _widget_evidence(
        self,
        wref: Any,
        wdict: DictionaryObject,
        terminal_key: Tuple[Any, ...],
        ftype: str,
        btn_value: Optional[str],
    ) -> WidgetEvidence:
        wkey = _key_of(wref)
        wdisp = _fmt_ref(wref)
        self.refnames.setdefault(wkey, wdisp)
        diags: List[str] = []

        # widget 归属（同一 widget 被两个终端字段引用 -> 冲突；
        # 合并 field+widget 时 widget 即字段自身，跳过）
        if wkey != terminal_key:
            if wkey in self.owner:
                prev = self.owner[wkey]
                if prev != terminal_key:
                    diags.append(
                        f"conflicting_ownership: widget {wdisp} also owned by "
                        f"{self.refnames.get(prev, '?')}"
                    )
                else:
                    diags.append(f"duplicate_widget: {wdisp} listed twice")
            else:
                self.owner[wkey] = terminal_key

        # widget 的 /Parent 应指向终端字段（合并 widget 即字段自身，跳过）
        if wkey != terminal_key:
            self._check_parent(wdict, terminal_key, None, wdisp)

        # 页号：以页面 Annots 扫描为准
        pages = self.scan.widget_pages.get(wkey, [])
        page = pages[0] if pages else None
        if not pages:
            diags.append("widget_not_on_any_page: not listed in any page /Annots")
        elif len(pages) > 1:
            diags.append(f"widget_on_multiple_pages: {pages}")

        # /P 交叉核对
        raw_p = _raw(wdict, "/P")
        if raw_p is not None:
            located = _locate_page(self.scan, raw_p)
            if located is None:
                diags.append(f"widget_P_not_a_page: {_fmt_ref(raw_p)}")
            elif pages and located != pages[0]:
                diags.append(
                    f"page_ref_mismatch: /P says page {located} but "
                    f"/Annots lists page {pages[0]}"
                )

        # Rect
        rect: Optional[List[float]] = None
        raw_rect = _resolve_safe(_raw(wdict, "/Rect"))
        if isinstance(raw_rect, ArrayObject) and len(raw_rect) == 4:
            try:
                rect = [float(_resolve_safe(x)) for x in raw_rect]
            except (TypeError, ValueError):
                diags.append("bad_rect")
                rect = None
        else:
            diags.append("missing_rect")

        # 外观状态集合与当前外观状态
        states: Optional[List[str]] = None
        ap = _resolve_safe(_raw(wdict, "/AP"))
        if isinstance(ap, DictionaryObject) and not _is_stream(ap):
            n = _resolve_safe(_raw(ap, "/N"))
            if isinstance(n, DictionaryObject) and not _is_stream(n):
                states = sorted(_name_str(k) for k in n.keys())
            # /N 为单一流外观：状态名不可枚举 -> 保持 None（unknown）
        as_raw = _resolve_safe(_raw(wdict, "/AS"))
        as_state: Optional[str] = None
        if isinstance(as_raw, NameObject):
            as_state = _name_str(as_raw)
        elif as_raw is not None:
            diags.append(f"odd_AS: {type(as_raw).__name__}")

        on_state: Optional[str] = None
        if states is not None and ftype in ("checkbox", "radio"):
            # 导出状态名只对按钮类 widget 有意义
            ons = [s for s in states if s != "Off"]
            if len(ons) == 1:
                on_state = ons[0]
            elif len(ons) > 1:
                diags.append(f"multiple_on_states: {ons}")

        evidence = _widget_evidence_state(ftype, btn_value, states, as_state)

        return WidgetEvidence(
            object_ref=wdisp,
            page=page,
            rect=rect,
            appearance_state=as_state,
            appearance_states=states,
            on_state=on_state,
            evidence=evidence,
            diagnostics=diags,
        )


def _resolve_safe(obj: Any) -> Any:
    try:
        return _resolve(obj)
    except Exception:
        return obj


def _classify(ft_raw: Any, flags: int) -> Tuple[str, List[str]]:
    if ft_raw is None:
        return "unknown", ["missing_FT: no /FT on the whole parent chain"]
    ft = str(_resolve_safe(ft_raw))
    if ft == "/Tx":
        return "text", []
    if ft == "/Ch":
        return "choice", []
    if ft == "/Btn":
        if flags & FF_BTN_PUSHBUTTON:
            return "unsupported", ["pushbutton_not_supported"]
        if flags & FF_BTN_RADIO:
            return "radio", []
        return "checkbox", []
    if ft == "/Sig":
        return "unsupported", ["signature_field_not_supported"]
    return "unsupported", [f"unsupported_FT:{ft}"]


def _text_value(v_raw: Any) -> Tuple[str, Optional[str], List[str]]:
    if v_raw is None:
        return "missing", None, []
    val = _resolve_safe(v_raw)
    if isinstance(val, NullObject):
        return "missing", None, ["null_V"]
    s, d = _text_str(val)
    diags = [d] if d else []
    if s is None:
        return "missing", None, diags + [f"unreadable_V:{type(val).__name__}"]
    return "filled", s, diags


def _text_default(dv_raw: Any) -> Tuple[Optional[str], List[str]]:
    if dv_raw is None:
        return None, []
    val = _resolve_safe(dv_raw)
    if isinstance(val, NullObject):
        return None, ["null_DV"]
    s, d = _text_str(val)
    diags = [f"DV:{d}"] if d else []
    return s, diags


def _btn_value(v_raw: Any) -> Tuple[str, Optional[str], List[str]]:
    """Checkbox/Radio 的逻辑值是导出状态名；Off 与缺值不同。"""
    if v_raw is None:
        return "missing", None, []
    val = _resolve_safe(v_raw)
    if isinstance(val, NullObject):
        return "missing", None, ["null_V"]
    if isinstance(val, NameObject):
        return "filled", _name_str(val), []
    if isinstance(val, TextStringObject):
        return "filled", str(val), ["text_V_on_button_field"]
    return "filled", str(val), [f"unexpected_V_type:{type(val).__name__}"]


def _btn_default(dv_raw: Any) -> Tuple[Optional[str], List[str]]:
    if dv_raw is None:
        return None, []
    val = _resolve_safe(dv_raw)
    if isinstance(val, NullObject):
        return None, ["null_DV"]
    if isinstance(val, NameObject):
        return _name_str(val), []
    s, d = _text_str(val)
    diags = [f"DV:{d}"] if d else []
    return s, diags


def _widget_evidence_state(
    ftype: str,
    btn_value: Optional[str],
    states: Optional[List[str]],
    as_state: Optional[str],
) -> str:
    """外观证据判定：只比较，不用外观覆盖逻辑值；无证据则 unknown。"""
    if ftype not in ("checkbox", "radio"):
        return "not_applicable"
    if states is None or as_state is None:
        return "unknown"          # 没有可靠外观证据，不猜测
    if btn_value is None:
        return "unknown"          # 逻辑值缺失，无法核对，不猜测
    on_states = [s for s in states if s != "Off"]
    expected = btn_value if btn_value in on_states else "Off"
    return "consistent" if as_state == expected else "stale"


def _summarize_appearance(evidences: List[WidgetEvidence], ftype: str) -> str:
    if ftype not in ("checkbox", "radio"):
        return "not_applicable"
    if not evidences:
        return "unknown"
    states = {ev.evidence for ev in evidences}
    if "stale" in states:
        return "stale"
    if "unknown" in states:
        return "unknown"
    return "consistent"


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------
def extract(source: Union[str, Path, BinaryIO]) -> ExtractionResult:
    """读取 PDF，产出字段数据及页面控件证据。

    字段环等结构性问题抛 AcroFormStructureError（明确失败，不给部分结果）；
    其余问题记入 diagnostics。纯函数，不修改任何共享状态。
    """
    reader, name = _open_reader(source)
    notes: List[str] = []
    fields = _acroform_fields(reader, notes)

    walker = _TreeWalker(reader)
    walker.walk(fields)  # 循环在此抛 AcroFormStructureError

    return ExtractionResult(
        source=name,
        page_count=len(reader.pages),
        fields=walker.records,
        diagnostics=notes + walker.diagnostics,
    )


class AcroFormExtractor:
    """有状态封装：只有成功的 extract 才更新 last_result。

    坏文件（如字段环）明确失败时，已有成功数据不被覆盖。
    """

    def __init__(self) -> None:
        self.last_result: Optional[ExtractionResult] = None

    def extract(self, source: Union[str, Path, BinaryIO]) -> ExtractionResult:
        result = extract(source)  # 失败会抛异常，last_result 保持不变
        self.last_result = result
        return result


# ---------------------------------------------------------------------------
# inspect：独立对象遍历核对字段数量与页面引用
# ---------------------------------------------------------------------------
@dataclass
class _TreeInfo:
    terminals: List[Tuple[Tuple[Any, ...], str]]          # (key, disp)
    widgets: Dict[Tuple[Any, ...], Tuple[str, Any]]       # widget key -> (owner disp, dict)
    node_count: int
    problems: List[str]


def _inspect_tree(reader: PdfReader) -> _TreeInfo:
    """独立于 extract 的迭代式字段树遍历（用于交叉核对，不复用其逻辑）。"""
    problems: List[str] = []
    terminals: List[Tuple[Tuple[Any, ...], str]] = []
    widgets: Dict[Tuple[Any, ...], Tuple[str, Any]] = {}
    owner: Dict[Tuple[Any, ...], Tuple[Any, ...]] = {}
    node_count = 0

    try:
        fields = _acroform_fields(reader)
    except AcroFormError as exc:
        return _TreeInfo([], {}, 0, [f"acroform_error: {exc}"])

    stack: List[Tuple[Any, Tuple[Any, ...], Tuple[Tuple[Any, ...], ...]]] = []
    for entry in fields:
        stack.append((entry, _ROOT_KEY, ()))
    owner[_ROOT_KEY] = _ROOT_KEY

    while stack:
        ref, parent_key, path = stack.pop()
        key = _key_of(ref)
        disp = _fmt_ref(ref)
        if key in path:
            problems.append(f"cycle_detected: {' -> '.join(_fmt_key(r) for r in path)} -> {disp}")
            continue
        if key in owner:
            if owner[key] != parent_key:
                problems.append(f"conflicting_ownership: {disp}")
            continue
        owner[key] = parent_key
        try:
            node = _resolve(ref)
        except Exception as exc:
            problems.append(f"broken_ref: {disp} ({exc})")
            continue
        if not isinstance(node, DictionaryObject) or _is_stream(node):
            problems.append(f"not_a_field_dict: {disp}")
            continue
        node_count += 1

        is_widget = str(_raw(node, "/Subtype")) == "/Widget"
        sub: List[Any] = []
        kids = _resolve_safe(_raw(node, "/Kids"))
        if isinstance(kids, ArrayObject):
            for kref in kids:
                kd = _resolve_safe(kref)
                if (
                    isinstance(kd, DictionaryObject)
                    and not _is_stream(kd)
                    and str(_raw(kd, "/Subtype")) == "/Widget"
                    and _raw(kd, "/T") is None
                    and _raw(kd, "/Kids") is None
                ):
                    widgets.setdefault(_key_of(kref), (disp, kd))
                else:
                    sub.append(kref)
        if is_widget:
            widgets.setdefault(key, (disp, node))
        if is_widget or not sub:
            terminals.append((key, disp))
        for kref in sub:
            stack.append((kref, key, path + (key,)))

    return _TreeInfo(terminals, widgets, node_count, problems)


def _scan_xref_widgets(reader: PdfReader) -> Dict[str, Any]:
    """全 xref 扫描：独立统计文件里的 widget 对象（不依赖字段树/Annots）。"""
    out: Dict[str, Any] = {"available": False}
    try:
        ids: List[Tuple[int, int]] = []
        xref = getattr(reader, "xref", None)
        if isinstance(xref, dict):
            for gen, table in xref.items():
                if gen == 65535:  # 自由项
                    continue
                for idnum in table:
                    ids.append((idnum, gen))
        objstm = getattr(reader, "xref_objStm", None)
        if isinstance(objstm, dict):
            for idnum in objstm:
                ids.append((idnum, 0))
        widgets: List[str] = []
        scanned = 0
        for idnum, gen in ids:
            try:
                obj = reader.get_object(IndirectObject(idnum, gen, reader))
            except Exception:
                continue
            scanned += 1
            if (
                isinstance(obj, DictionaryObject)
                and not _is_stream(obj)
                and str(_raw(obj, "/Subtype")) == "/Widget"
            ):
                widgets.append(f"{idnum} {gen} R")
        out.update(
            available=True,
            objects_scanned=scanned,
            widget_objects=sorted(widgets),
        )
    except Exception as exc:  # pragma: no cover - 防御性
        out["error"] = str(exc)
    return out


def inspect(
    source: Union[str, Path, BinaryIO],
    result: Optional[ExtractionResult] = None,
) -> Dict[str, Any]:
    """独立核对：字段数量、widget 页面引用、对象集合交叉验证。

    不调用 get_fields() 作为数据来源（仅作对照注记），不原样输出。
    """
    reader, name = _open_reader(source)
    problems: List[str] = []
    warnings: List[str] = []

    tree = _inspect_tree(reader)
    problems.extend(tree.problems)
    scan = _scan_annots(reader)

    tree_widget_keys = set(tree.widgets.keys())
    annot_widget_keys = set(scan.widget_pages.keys())

    not_on_page: List[str] = []
    for k in sorted(tree_widget_keys - annot_widget_keys, key=str):
        owner_disp, _d = tree.widgets[k]
        not_on_page.append(f"{scan.widget_refs.get(k, _fmt_key(k))} (field {owner_disp})")
    if not_on_page:
        problems.append(f"widgets_not_on_any_page: {not_on_page}")

    orphan = sorted(
        scan.widget_refs.get(k, _fmt_key(k)) for k in annot_widget_keys - tree_widget_keys
    )
    if orphan:
        problems.append(f"orphan_widget_annots: {orphan}")

    # /P 与 Annots 页码交叉核对
    page_mismatches: List[str] = []
    for k in sorted(tree_widget_keys & annot_widget_keys, key=str):
        _owner_disp, wdict = tree.widgets[k]
        raw_p = _raw(wdict, "/P")
        if raw_p is None:
            continue
        located = _locate_page(scan, raw_p)
        annot_page = scan.widget_pages[k][0]
        if located is not None and located != annot_page:
            page_mismatches.append(
                f"{scan.widget_refs.get(k, _fmt_key(k))}: /P=page {located}, "
                f"/Annots=page {annot_page}"
            )
    if page_mismatches:
        problems.append(f"page_ref_mismatches: {page_mismatches}")

    raw = _scan_xref_widgets(reader)
    if raw.get("available"):
        annot_refs = {scan.widget_refs.get(k, "") for k in annot_widget_keys}
        unreachable = [w for w in raw["widget_objects"] if w not in annot_refs]
        if unreachable:
            warnings.append(f"widget_objects_not_reachable_from_annots: {unreachable}")

    # get_fields() 仅作对照注记（pypdf 会把非终端字段也算进去）
    get_fields_count: Union[int, str] = "unavailable"
    try:
        get_fields_count = len(reader.get_fields() or {})
        if get_fields_count != len(tree.terminals):
            warnings.append(
                f"get_fields_count={get_fields_count} != terminal_fields="
                f"{len(tree.terminals)} (get_fields 口径不同，仅注记)"
            )
    except Exception as exc:
        warnings.append(f"get_fields_failed: {exc}")

    report: Dict[str, Any] = {
        "source": name,
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "terminal_fields": len(tree.terminals),
        "field_nodes_total": tree.node_count,
        "widgets_in_tree": len(tree_widget_keys),
        "widgets_in_annots": len(annot_widget_keys),
        "widgets_not_on_any_page": not_on_page,
        "orphan_widget_annots": orphan,
        "page_ref_mismatches": page_mismatches,
        "xref_scan": raw,
        "get_fields_count": get_fields_count,
    }

    if result is not None:
        report["records_extracted"] = len(result.fields)
        if len(result.fields) != len(tree.terminals):
            problems.append(
                f"record_count_mismatch: extract={len(result.fields)} "
                f"tree={len(tree.terminals)}"
            )
            report["ok"] = False
        # extract 的 widget 引用集合与独立遍历一致？
        result_widget_refs = {
            w.object_ref for f in result.fields for w in f.widgets
        }
        tree_widget_refs = {
            scan.widget_refs.get(k) or _fmt_key(k) for k in tree_widget_keys
        }
        if result_widget_refs != tree_widget_refs:
            problems.append(
                "widget_ref_set_mismatch: "
                f"extract={sorted(result_widget_refs)} tree={sorted(tree_widget_refs)}"
            )
            report["ok"] = False
        # 每条记录的 widget 页号与 Annots 扫描一致？
        page_errors: List[str] = []
        annot_page_by_ref = {scan.widget_refs.get(k): v for k, v in scan.widget_pages.items()}
        for f in result.fields:
            for w in f.widgets:
                expect = annot_page_by_ref.get(w.object_ref)
                if expect and w.page != expect[0]:
                    page_errors.append(
                        f"{f.name}: widget {w.object_ref} page {w.page} != annots {expect[0]}"
                    )
        if page_errors:
            problems.append(f"widget_page_mismatch: {page_errors}")
            report["ok"] = False

    report["ok"] = not problems
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import logging

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # 抑制 pypdf 警告噪声

    parser = argparse.ArgumentParser(
        prog="acroform_extract",
        description="AcroForm 填写值采集（只读；不支持 XFA/签名/脚本）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_extract = sub.add_parser("extract", help="输出字段数据及页面控件证据 JSON")
    p_extract.add_argument("pdf")
    p_inspect = sub.add_parser("inspect", help="独立遍历核对字段数量与页面引用")
    p_inspect.add_argument("pdf")
    args = parser.parse_args(argv)

    if args.cmd == "extract":
        try:
            result = extract(args.pdf)
        except AcroFormError as exc:
            print(
                json.dumps(
                    {"ok": False, "error": str(exc), "diagnostics": exc.diagnostics},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        print(result.to_json())
        return 0

    # inspect：即使 extract 失败（如字段环）也输出独立核对报告
    result: Optional[ExtractionResult] = None
    extract_error: Optional[str] = None
    try:
        result = extract(args.pdf)
    except AcroFormError as exc:
        extract_error = str(exc)
    report = inspect(args.pdf, result=result)
    if extract_error is not None:
        report["extract_error"] = extract_error
        report["ok"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
