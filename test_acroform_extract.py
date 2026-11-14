"""acroform_extract 的核对测试。

主例使用给定真实 PDF：fixtures/forms.pdf、fixtures/bad-cycle.pdf；
补充情形（Off/缺值、多选、V/I 不一致、断裂父链、归属冲突等）使用
make_extra_fixtures 本地合成的 PDF。

运行：.venv/bin/python -m pytest test_acroform_extract.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acroform_extract import (
    AcroFormExtractor,
    AcroFormStructureError,
    extract,
    inspect,
)
from make_extra_fixtures import build_all

ROOT = Path(__file__).parent
FORMS = ROOT / "fixtures" / "forms.pdf"
BAD_CYCLE = ROOT / "fixtures" / "bad-cycle.pdf"


@pytest.fixture(scope="module")
def forms_result():
    return extract(FORMS)


@pytest.fixture(scope="module")
def extra(tmp_path_factory):
    out = tmp_path_factory.mktemp("extra")
    return build_all(out)


def by_name(result, name):
    matches = [f for f in result.fields if f.name == name]
    assert len(matches) == 1, f"field {name!r} not unique: {matches}"
    return matches[0]


# ---------------------------------------------------------------------------
# fixtures/forms.pdf（给定真实 PDF）
# ---------------------------------------------------------------------------
class TestFormsPdf:
    def test_five_terminal_records(self, forms_result):
        # 一个终端字段一条记录，无论多少 widget
        assert forms_result.field_count == 5
        assert {f.name for f in forms_result.fields} == {
            "contact.name", "consent", "choice", "default_only", "radio",
        }

    def test_cross_page_widgets_single_record(self, forms_result):
        # 同一终端字段的两个 widget 分布在两页，仍只产生一条记录
        f = by_name(forms_result, "contact.name")
        assert len(f.widgets) == 2
        assert {w.page for w in f.widgets} == {1, 2}
        assert all(w.rect and len(w.rect) == 4 for w in f.widgets)
        assert len({w.object_ref for w in f.widgets}) == 2  # 各自对象引用

    def test_inherited_text_type(self, forms_result):
        # /FT 在父节点 contact 上，终端 name 继承
        f = by_name(forms_result, "contact.name")
        assert f.field_type == "text"
        assert f.sources["ft"] == f"inherited:{f.node_refs[0]}"
        assert f.sources["ft"] != "self"

    def test_unicode_value(self, forms_result):
        f = by_name(forms_result, "contact.name")
        assert f.status == "filled"
        assert f.value == "李明"  # 真实 Unicode，非转义/占位
        # JSON 往返不丢字
        again = json.loads(forms_result.to_json())
        assert again["fields"][0]["value"] == "李明"

    def test_name_segments_and_object_identity(self, forms_result):
        f = by_name(forms_result, "contact.name")
        assert f.name_segments == ["contact", "name"]
        assert len(f.node_refs) == 2
        assert f.terminal_ref == f.node_refs[-1]
        assert all(ref.endswith(" R") for ref in f.node_refs)

    def test_default_value_not_faked_as_filled(self, forms_result):
        # 只有 DV 没有 V：记 missing，绝不用默认值冒充已填写
        f = by_name(forms_result, "default_only")
        assert f.status == "missing"
        assert f.value is None
        assert f.default_value == "尚未填写默认值"

    def test_choice_export_vs_display(self, forms_result):
        f = by_name(forms_result, "choice")
        assert f.field_type == "choice"
        assert f.value == "b"                       # 导出值
        assert f.value_labels == ["乙"]             # 显示标签
        assert f.options == [
            {"export": "a", "display": "甲"},
            {"export": "b", "display": "乙"},
        ]
        assert f.indices == [1]                     # /I 与 V 一致
        assert not any("mismatch" in d for d in f.diagnostics)

    def test_radio_group_is_one_field(self, forms_result):
        # Radio 组是一个字段，各按钮不拆成多个字段
        f = by_name(forms_result, "radio")
        assert f.field_type == "radio"
        assert f.flags & (1 << 15)                  # Radio 标志位
        assert f.value == "B"                       # 导出状态名
        assert len(f.widgets) == 2
        assert {w.page for w in f.widgets} == {1, 2}
        assert f.export_states == ["A", "B"]
        on = {w.on_state for w in f.widgets}
        assert on == {"A", "B"}

    def test_stale_appearance_keeps_logical_value(self, forms_result):
        # consent: V=/Yes 但 widget AS=/Off —— 保留逻辑值，标 appearance_mismatch，
        # 不用视觉状态覆盖字段值，也不声称外观一致
        f = by_name(forms_result, "consent")
        assert f.field_type == "checkbox"
        assert f.value == "Yes"
        assert f.appearance_mismatch is True
        assert f.appearance == "stale"
        w = f.widgets[0]
        assert w.evidence == "stale"
        assert w.appearance_state == "Off"
        assert w.appearance_states == ["Off", "Yes"]

    def test_inspect_cross_check(self, forms_result):
        report = inspect(FORMS, result=forms_result)
        assert report["ok"] is True
        assert report["problems"] == []
        assert report["terminal_fields"] == 5       # 独立遍历核对字段数量
        assert report["records_extracted"] == 5
        assert report["widgets_in_tree"] == report["widgets_in_annots"] == 7
        assert report["xref_scan"]["available"] is True
        assert len(report["xref_scan"]["widget_objects"]) == 7
        # get_fields 仅作对照（pypdf 会把非终端 contact 也算进去）
        assert report["get_fields_count"] == 6
        assert any("get_fields" in w for w in report["warnings"])


# ---------------------------------------------------------------------------
# fixtures/bad-cycle.pdf（给定坏例：字段环）
# ---------------------------------------------------------------------------
class TestBadCycle:
    def test_cycle_fails_clearly(self):
        with pytest.raises(AcroFormStructureError) as ei:
            extract(BAD_CYCLE)
        assert "cycle" in str(ei.value).lower()
        assert ei.value.diagnostics

    def test_failure_does_not_clobber_good_data(self):
        # 已有成功数据不被坏文件覆盖
        extractor = AcroFormExtractor()
        good = extractor.extract(FORMS)
        assert extractor.last_result is good
        with pytest.raises(AcroFormStructureError):
            extractor.extract(BAD_CYCLE)
        assert extractor.last_result is good  # 仍是上次成功结果

    def test_inspect_reports_cycle_without_hanging(self):
        report = inspect(BAD_CYCLE)
        assert report["ok"] is False
        assert any("cycle" in p for p in report["problems"])


# ---------------------------------------------------------------------------
# 补充合成 PDF：给定样例未覆盖的情形
# ---------------------------------------------------------------------------
class TestExtraStates:
    def test_off_is_not_missing(self, extra):
        result = extract(extra["states_form"])
        off = by_name(result, "cb_off")
        assert off.status == "filled"
        assert off.value == "Off"                   # 显式 Off 是已填写的导出状态
        assert off.appearance_mismatch is False
        unset = by_name(result, "cb_unset")
        assert unset.status == "missing"            # 缺 V 才是 missing
        assert unset.value is None
        # 缺 V 时无法核对 -> unknown，不猜测
        assert unset.widgets[0].evidence == "unknown"

    def test_multi_select_choice(self, extra):
        result = extract(extra["states_form"])
        f = by_name(result, "multi")
        assert f.multi_select is True
        assert f.value == ["a", "c"]
        assert f.value_labels == ["甲", "丙"]
        assert f.indices == [0, 2]
        assert not any("mismatch" in d for d in f.diagnostics)

    def test_v_i_mismatch_keeps_v(self, extra):
        result = extract(extra["states_form"])
        f = by_name(result, "vi_bad")
        assert f.value == "Y"                       # 保留逻辑 V
        assert any("v_i_mismatch" in d for d in f.diagnostics)

    def test_merged_field_widget(self, extra):
        result = extract(extra["states_form"])
        f = by_name(result, "merged")
        assert f.value == "合并部件值"
        assert len(f.widgets) == 1
        assert f.widgets[0].object_ref == f.terminal_ref  # 字段自身即 widget
        assert f.widgets[0].page == 1
        assert f.widgets[0].diagnostics == []

    def test_ghost_field_no_widgets(self, extra):
        result = extract(extra["states_form"])
        f = by_name(result, "ghost")
        assert f.widgets == []
        assert any("no_widgets" in d for d in f.diagnostics)

    def test_orphan_widget_not_on_page(self, extra):
        result = extract(extra["states_form"])
        f = by_name(result, "orphan")
        assert f.widgets[0].page is None
        assert any("widget_not_on_any_page" in d for d in f.widgets[0].diagnostics)
        # inspect 独立核对同样发现
        report = inspect(extra["states_form"], result=result)
        assert report["ok"] is False
        assert any("widgets_not_on_any_page" in p for p in report["problems"])

    def test_unsupported_types_reported_not_crashing(self, extra):
        result = extract(extra["states_form"])
        sig = by_name(result, "sig")
        assert sig.field_type == "unsupported"
        assert sig.status == "unsupported"
        assert any("signature" in d for d in sig.diagnostics)
        push = by_name(result, "push")
        assert push.field_type == "unsupported"
        assert any("pushbutton" in d for d in push.diagnostics)

    def test_flags_inherited(self, extra):
        result = extract(extra["states_form"])
        f = by_name(result, "ro_group.ro_text")
        assert f.flags == 1                          # ReadOnly 从父链继承
        assert f.sources["ff"].startswith("inherited:")
        assert f.sources["ft"].startswith("inherited:")


class TestBrokenParent:
    def test_parent_mismatch_and_dangling_ref_diagnosed(self, extra):
        result = extract(extra["broken_parent"])
        # 断裂父链记诊断，但不中断采集
        assert any("parent_mismatch" in d for d in result.diagnostics)
        assert any("broken_parent_ref" in d for d in result.diagnostics)
        kid = by_name(result, "root_a.kid")
        assert kid.value == "k"
        assert kid.field_type == "text"
        assert kid.sources["ft"].startswith("inherited:")


class TestConflictingOwnership:
    def test_conflict_diagnosed_single_record(self, extra):
        result = extract(extra["conflicting"])
        assert any("conflicting_ownership" in d for d in result.diagnostics)
        shared = [f for f in result.fields if f.name.endswith("shared")]
        assert len(shared) == 1                      # 不重复产出
        assert shared[0].value == "共享字段"
        report = inspect(extra["conflicting"])
        assert report["ok"] is False
        assert any("conflicting_ownership" in p for p in report["problems"])


# ---------------------------------------------------------------------------
# 边界与范围
# ---------------------------------------------------------------------------
class TestBoundaries:
    def test_extract_from_stream(self, forms_result):
        with open(FORMS, "rb") as fh:
            result = extract(fh)
        assert result.field_count == forms_result.field_count
        assert [f.name for f in result.fields] == [f.name for f in forms_result.fields]

    def test_no_acroform_raises(self, tmp_path):
        from pypdf import PdfWriter

        from acroform_extract import AcroFormError
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        path = tmp_path / "blank.pdf"
        with open(path, "wb") as fh:
            writer.write(fh)
        with pytest.raises(AcroFormError, match="AcroForm"):
            extract(path)

    def test_xfa_rejected(self, tmp_path):
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import ArrayObject, NameObject, TextStringObject

        from acroform_extract import AcroFormError
        reader = PdfReader(str(FORMS))
        writer = PdfWriter()
        writer.append(reader)
        acro = writer._root_object["/AcroForm"].get_object()
        acro[NameObject("/XFA")] = ArrayObject([TextStringObject("preamble")])
        path = tmp_path / "xfa.pdf"
        with open(path, "wb") as fh:
            writer.write(fh)
        with pytest.raises(AcroFormError, match="XFA"):
            extract(path)


# ---------------------------------------------------------------------------
# 输出形态
# ---------------------------------------------------------------------------
class TestOutputShape:
    def test_json_roundtrip(self, forms_result):
        payload = json.loads(forms_result.to_json())
        assert payload["field_count"] == 5
        assert payload["page_count"] == 2
        for f in payload["fields"]:
            assert {"name", "name_segments", "node_refs", "terminal_ref",
                    "field_type", "status", "value", "widgets"} <= set(f)
            for w in f["widgets"]:
                assert {"object_ref", "page", "rect", "appearance_state",
                        "appearance_states", "evidence"} <= set(w)

    def test_widget_evidence_fields(self, forms_result):
        # 每个 widget 保留页号、Rect、对象引用及可用状态集合
        for f in forms_result.fields:
            for w in f.widgets:
                assert isinstance(w.object_ref, str) and w.object_ref
                assert isinstance(w.page, int) and w.page >= 1
                assert w.rect is not None and len(w.rect) == 4
                assert w.appearance_states is not None
