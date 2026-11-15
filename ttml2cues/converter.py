"""TTML 层级时钟解析与时间片切分。

流程严格对应 timing.md：
1. 先按 par/seq 层级时钟算每个元素的 *未裁切* 区间（同步基准 + begin；
   end 与同步基准同源，dur 从本项 start 计时）。
2. 再与全部祖先的有效区间取交；裁切不回写、不改变任何相对时钟，
   seq 推进永远使用兄弟的未裁切结束。
3. 收集 p 内全部有效文本节点 / tail / br，按各自直接所属元素的有效
   区间在 p 的有效时段内切最大稳定片段。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

from .timing import TICK_RATE, TimingError, parse_time_expression

NS_TT = "http://www.w3.org/ns/ttml"
NS_TTP = "http://www.w3.org/ns/ttml#parameter"
NS_XML = "http://www.w3.org/XML/1998/namespace"

TIMING_TAGS = {"body", "div", "p", "span"}
TEXT_CONTAINER_TAGS = {"p", "span"}


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _attr(el: ET.Element, name: str) -> str | None:
    return el.get(name)


@dataclass
class ElementInfo:
    element: ET.Element
    local: str
    xml_id: str | None
    xml_path: str
    stable_id: str
    time_container: str  # "par" | "seq"
    sync_base_tick: int | None = None
    sync_base_kind: str | None = None  # root | par | seq
    begin_tick: int | None = None
    end_offset_tick: int | None = None  # end 相对同步基准
    duration_tick: int | None = None
    unclipped_start: int | None = None
    unclipped_end: int | None = None
    effective_start: int | None = None
    effective_end: int | None = None
    clipped_out: bool = False
    children: list["ElementInfo"] = field(default_factory=list)
    parent: "ElementInfo | None" = None

    @property
    def effective_interval(self) -> tuple[int, int] | None:
        if self.effective_start is None:
            return None
        return self.effective_start, self.effective_end  # type: ignore[return-value]


@dataclass
class TextUnit:
    source_node_id: str
    p_id: str
    kind: str  # text | tail | br
    xml_path: str
    text: str
    owner_stable_id: str
    ordinal: int
    # 直接所属元素的有效区间（tail 属于前置元素的父元素）
    start_tick: int | None
    end_tick: int | None


@dataclass
class ParagraphRecord:
    info: ElementInfo
    empty_fragment_count: int = 0
    empty_fragments: list[dict] = field(default_factory=list)


def merge_adjacent_fragments(fragments: list[dict]) -> list[dict]:
    """合并相邻片段：仅当文本与参与源节点 ID 序列都相同才合并，输出最大稳定片段。"""
    merged: list[dict] = []
    for frag in fragments:
        if (
            merged
            and merged[-1]["end_tick"] == frag["start_tick"]
            and merged[-1]["text"] == frag["text"]
            and merged[-1]["source_node_ids"] == frag["source_node_ids"]
        ):
            merged[-1]["end_tick"] = frag["end_tick"]
        else:
            merged.append(dict(frag))
    return merged


class TTMLConverter:
    def __init__(self, document_path: str, tick_rate: int = TICK_RATE):
        self.document_path = document_path
        self.document_name = os.path.basename(document_path)
        self.tick_rate = tick_rate
        try:
            self.tree = ET.parse(document_path)
        except ET.ParseError as exc:
            raise TimingError(f"XML 解析失败: {exc}") from exc
        self.root = self.tree.getroot()
        self.infos: dict[ET.Element, ElementInfo] = {}
        self.paragraphs: list[ParagraphRecord] = []
        self.text_units: list[TextUnit] = []
        self.cues: list[dict] = []

    # ------------------------------------------------------------------ #
    # 第一层：层级时钟
    # ------------------------------------------------------------------ #
    def resolve(self) -> None:
        if _local(self.root.tag) != "tt":
            raise TimingError("根元素必须是 tt")
        rate_attr = _attr(self.root, f"{{{NS_TTP}}}tickRate")
        if rate_attr is not None and rate_attr.strip() != str(self.tick_rate):
            raise TimingError(
                f"只支持 tickRate={self.tick_rate}，文档声明 {rate_attr!r}"
            )
        body = self._unique_child(self.root, "body")
        if _attr(body, "dur") is None:
            raise TimingError("body 必须有有限 dur")
        body_info = self._resolve_element(
            body,
            sync_base=0,
            sync_kind="root",
            ancestors=[],
            parent_path="",
            same_name_index=1,
            parent_info=None,
        )
        if body_info.unclipped_end is None:
            raise TimingError("body 必须有有限 dur")
        self.body_info = body_info

    def _unique_child(self, parent: ET.Element, local: str) -> ET.Element:
        matches = [c for c in parent if isinstance(c.tag, str) and _local(c.tag) == local]
        if len(matches) != 1:
            raise TimingError(f"元素 {parent.tag} 下应恰有一个 {local}")
        return matches[0]

    def _resolve_element(
        self,
        el: ET.Element,
        sync_base: int,
        sync_kind: str,
        ancestors: list[tuple[int, int]],
        parent_path: str,
        same_name_index: int,
        parent_info: ElementInfo | None,
    ) -> ElementInfo:
        local = _local(el.tag)
        if local not in TIMING_TAGS and local != "br":
            raise TimingError(
                f"不支持的元素 {local}（路径见 {parent_path}）：子集只允许 body/div/p/span/br"
            )

        xml_id = _attr(el, f"{{{NS_XML}}}id")
        path = f"{parent_path}/{local}[{same_name_index}]"
        if xml_id is not None:
            path += f"[@xml:id='{xml_id}']"
        if local == "br":
            base = parent_info.stable_id if parent_info is not None else "root"
            stable_id = xml_id or f"{base}#br-{same_name_index}"
        else:
            stable_id = xml_id or "node:" + path.lstrip("/").replace("/", ".")

        tc_attr = _attr(el, "timeContainer")
        if tc_attr is None:
            time_container = "par"
        elif tc_attr in ("par", "seq"):
            time_container = tc_attr
        else:
            raise TimingError(f"{path}: 不支持的 timeContainer={tc_attr!r}")

        info = ElementInfo(
            element=el,
            local=local,
            xml_id=xml_id,
            xml_path=path,
            stable_id=stable_id,
            time_container=time_container,
        )
        self.infos[el] = info

        if local == "br":
            # br 不参与定时，只在文本收集阶段按父元素时段可见
            return info

        info.sync_base_tick = sync_base
        info.sync_base_kind = sync_kind
        begin_raw = _attr(el, "begin")
        info.begin_tick = (
            parse_time_expression(begin_raw, self.tick_rate)
            if begin_raw is not None
            else 0
        )

        end_raw = _attr(el, "end")
        dur_raw = _attr(el, "dur")
        if end_raw is not None:
            info.end_offset_tick = parse_time_expression(end_raw, self.tick_rate)
        if dur_raw is not None:
            info.duration_tick = parse_time_expression(dur_raw, self.tick_rate)

        if sync_kind == "seq" and end_raw is None and dur_raw is None:
            raise TimingError(f"{path}: seq 容器的直接孩子必须显式给出 end 或 dur")

        start = sync_base + info.begin_tick
        ends: list[int] = []
        if info.end_offset_tick is not None:
            # end 与 begin 同一同步基准
            ends.append(sync_base + info.end_offset_tick)
        if info.duration_tick is not None:
            # dur 从本项未裁切 start 计时
            ends.append(start + info.duration_tick)
        if not ends:
            if local == "body":
                raise TimingError(f"{path}: body 必须有有限 dur")
            if parent_info is None or parent_info.time_container != "par":
                raise TimingError(
                    f"{path}: 缺少 end/dur 的元素只能作为 par 孩子继承父未裁切结束"
                )
            end = parent_info.unclipped_end
            assert end is not None
        else:
            end = min(ends)

        if end < start:
            raise TimingError(
                f"{path}: 结束 tick {end} 早于开始 tick {start}，不允许 end<start"
            )

        info.unclipped_start = start
        info.unclipped_end = end

        # 与全部祖先有效区间取交；祖先为空则本项必为空，但相对时钟保持未裁切值。
        lo, hi = start, end
        for a_lo, a_hi in ancestors:
            lo = max(lo, a_lo)
            hi = min(hi, a_hi)
        if lo >= hi:
            info.clipped_out = True
        else:
            info.effective_start = lo
            info.effective_end = hi

        self._resolve_children(info, ancestors)

        if local == "p":
            self.paragraphs.append(ParagraphRecord(info=info))
        return info

    def _resolve_children(
        self, info: ElementInfo, ancestors: list[tuple[int, int]]
    ) -> None:
        el = info.element
        child_ancestors = ancestors + (
            [info.effective_interval] if info.effective_interval is not None else []
        )
        name_counts: dict[str, int] = {}
        if info.time_container == "seq":
            # 第一个孩子的基准是父项未裁切开始；之后用前一兄弟的未裁切结束。
            prev_end = info.unclipped_start
            assert prev_end is not None
            for child in el:
                if not isinstance(child.tag, str):
                    continue
                child_local = _local(child.tag)
                if child_local not in TIMING_TAGS and child_local != "br":
                    raise TimingError(
                        f"{info.xml_path}: 不支持的子元素 {child_local}"
                    )
                name_counts[child_local] = name_counts.get(child_local, 0) + 1
                child_info = self._resolve_element(
                    child,
                    sync_base=prev_end,
                    sync_kind="seq",
                    ancestors=child_ancestors,
                    parent_path=info.xml_path,
                    same_name_index=name_counts[child_local],
                    parent_info=info,
                )
                info.children.append(child_info)
                child_info.parent = info
                if child_local != "br":
                    # 裁切不影响 seq 推进：始终用未裁切结束。
                    assert child_info.unclipped_end is not None
                    prev_end = child_info.unclipped_end
        else:
            for child in el:
                if not isinstance(child.tag, str):
                    continue
                child_local = _local(child.tag)
                if child_local not in TIMING_TAGS and child_local != "br":
                    raise TimingError(
                        f"{info.xml_path}: 不支持的子元素 {child_local}"
                    )
                name_counts[child_local] = name_counts.get(child_local, 0) + 1
                child_info = self._resolve_element(
                    child,
                    sync_base=info.unclipped_start,  # par 基准=父未裁切开始
                    sync_kind="par",
                    ancestors=child_ancestors,
                    parent_path=info.xml_path,
                    same_name_index=name_counts[child_local],
                    parent_info=info,
                )
                info.children.append(child_info)
                child_info.parent = info

    # ------------------------------------------------------------------ #
    # 第二层：p 内文本单元收集与时间片切分
    # ------------------------------------------------------------------ #
    def collect_text_units(self) -> None:
        for record in self.paragraphs:
            self._collect_from_element(record.info, record, ordinal=0)

    def _collect_from_element(
        self,
        el_info: ElementInfo,
        record: ParagraphRecord,
        ordinal: int,
    ) -> int:
        el = el_info.element
        # 元素自身 .text：继承该元素的有效区间
        if el.text:
            ordinal += 1
            self._append_text_unit(
                record=record,
                kind="text",
                content=el.text,
                carrier=el_info,
                interval_owner=el_info,
                path=f"{el_info.xml_path}/text()[1]",
                ordinal=ordinal,
            )
        for child in el:
            if not isinstance(child.tag, str):
                continue
            child_info = self.infos[child]
            child_local = child_info.local
            if child_local == "br":
                ordinal += 1
                # br 固定换行，可见时段取直接父元素的有效区间
                self.text_units.append(
                    TextUnit(
                        source_node_id=child_info.stable_id,
                        p_id=record.info.stable_id,
                        kind="br",
                        xml_path=child_info.xml_path,
                        text="\n",
                        owner_stable_id=el_info.stable_id,
                        ordinal=ordinal,
                        start_tick=el_info.effective_start,
                        end_tick=el_info.effective_end,
                    )
                )
            elif child_local in TEXT_CONTAINER_TAGS:
                ordinal = self._collect_from_element(child_info, record, ordinal)
            else:
                raise TimingError(
                    f"{child_info.xml_path}: p 内不支持的元素 {child_local}"
                )
            # 子元素 tail 属于 *父元素*，按父元素时段可见
            if child.tail:
                ordinal += 1
                self._append_text_unit(
                    record=record,
                    kind="tail",
                    content=child.tail,
                    carrier=child_info,
                    interval_owner=el_info,
                    path=f"{child_info.xml_path}/tail()[1]",
                    ordinal=ordinal,
                )
        return ordinal

    def _append_text_unit(
        self,
        record: ParagraphRecord,
        kind: str,
        content: str,
        carrier: ElementInfo,
        interval_owner: ElementInfo,
        path: str,
        ordinal: int,
    ) -> None:
        if kind == "text":
            node_id = f"{carrier.stable_id}#text-{ordinal}"
        else:
            node_id = f"{carrier.stable_id}#tail-{ordinal}"
        lo = interval_owner.effective_start
        hi = interval_owner.effective_end
        # 再与 p 有效区间夹一次（祖先裁切后理论上已包含，这里保证不变式）
        p_interval = record.info.effective_interval
        if p_interval is not None and lo is not None and hi is not None:
            p_lo, p_hi = p_interval
            lo2, hi2 = max(lo, p_lo), min(hi, p_hi)
            if lo2 >= hi2:
                lo = hi = None
            else:
                lo, hi = lo2, hi2
        self.text_units.append(
            TextUnit(
                source_node_id=node_id,
                p_id=record.info.stable_id,
                kind=kind,
                xml_path=path,
                text=content,
                owner_stable_id=interval_owner.stable_id,
                ordinal=ordinal,
                start_tick=lo,
                end_tick=hi,
            )
        )

    def build_cues(self) -> None:
        cue_seq = 0
        for record in self.paragraphs:
            p_info = record.info
            p_interval = p_info.effective_interval
            if p_interval is None:
                continue  # 完全裁空：合法不可见，诊断保留在 paragraphs 里
            p_lo, p_hi = p_interval
            units = [u for u in self.text_units if u.p_id == p_info.stable_id]
            bounds = {p_lo, p_hi}
            for u in units:
                if u.start_tick is None or u.end_tick is None:
                    continue
                lo = max(u.start_tick, p_lo)
                hi = min(u.end_tick, p_hi)
                if lo >= hi:
                    continue
                bounds.add(lo)
                bounds.add(hi)
            timeline = sorted(bounds)
            fragments: list[dict] = []
            for a, b in zip(timeline, timeline[1:]):
                midpoint = (a + b) // 2  # 相邻边界中点独立判可见
                visible = [
                    u
                    for u in units
                    if u.start_tick is not None
                    and u.end_tick is not None
                    and u.start_tick <= midpoint < u.end_tick
                ]
                text = "".join(u.text for u in visible)
                node_ids = [u.source_node_id for u in visible]
                if text == "":
                    record.empty_fragment_count += 1
                    record.empty_fragments.append(
                        {
                            "start_tick": a,
                            "end_tick": b,
                            "source_node_ids": node_ids,
                        }
                    )
                    continue
                fragments.append(
                    {
                        "start_tick": a,
                        "end_tick": b,
                        "text": text,
                        "source_node_ids": node_ids,
                    }
                )
            # 仅当文本与参与源节点序列都相同且相邻时合并为最大稳定片段
            merged = merge_adjacent_fragments(fragments)
            for frag in merged:
                cue_seq += 1
                self.cues.append(
                    {
                        "cue_id": f"cue-{cue_seq:04d}",
                        "p_id": p_info.stable_id,
                        "start_tick": frag["start_tick"],
                        "end_tick": frag["end_tick"],
                        "text": frag["text"],
                        "source_node_ids": list(frag["source_node_ids"]),
                    }
                )

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def write_outputs(self, out_dir: str) -> dict[str, str]:
        os.makedirs(out_dir, exist_ok=True)
        paths = {
            "paragraphs": os.path.join(out_dir, "paragraphs.jsonl"),
            "cues": os.path.join(out_dir, "cues.jsonl"),
            "source_nodes": os.path.join(out_dir, "source_nodes.jsonl"),
        }
        with open(paths["paragraphs"], "w", encoding="utf-8") as fh:
            for record in self.paragraphs:
                fh.write(json.dumps(self._paragraph_row(record), ensure_ascii=False) + "\n")
        with open(paths["cues"], "w", encoding="utf-8") as fh:
            for cue in self.cues:
                fh.write(json.dumps(cue, ensure_ascii=False) + "\n")
        with open(paths["source_nodes"], "w", encoding="utf-8") as fh:
            for unit in self.text_units:
                fh.write(json.dumps(self._source_node_row(unit), ensure_ascii=False) + "\n")
        return paths

    def _paragraph_row(self, record: ParagraphRecord) -> dict:
        info = record.info
        return {
            "document": self.document_name,
            "p_id": info.stable_id,
            "xml_id": info.xml_id,
            "xml_path": info.xml_path,
            "time_container": info.time_container,
            "sync_base_kind": info.sync_base_kind,
            "sync_base_tick": info.sync_base_tick,
            "begin_tick": info.begin_tick,
            "end_offset_tick": info.end_offset_tick,
            "duration_tick": info.duration_tick,
            "unclipped_start_tick": info.unclipped_start,
            "unclipped_end_tick": info.unclipped_end,
            "effective_start_tick": info.effective_start,
            "effective_end_tick": info.effective_end,
            "visible": info.effective_interval is not None,
            "clipped_out": info.clipped_out,
            "empty_text_fragment_count": record.empty_fragment_count,
            "empty_text_fragments": record.empty_fragments,
        }

    def _source_node_row(self, unit: TextUnit) -> dict:
        return {
            "document": self.document_name,
            "source_node_id": unit.source_node_id,
            "p_id": unit.p_id,
            "kind": unit.kind,
            "xml_path": unit.xml_path,
            "owner_element_id": unit.owner_stable_id,
            "ordinal": unit.ordinal,
            "text": unit.text,
            "effective_start_tick": unit.start_tick,
            "effective_end_tick": unit.end_tick,
        }

    def run(self, out_dir: str) -> dict[str, str]:
        self.resolve()
        self.collect_text_units()
        self.build_cues()
        return self.write_outputs(out_dir)


def convert_file(input_path: str, out_dir: str, tick_rate: int = TICK_RATE) -> dict[str, str]:
    """转换入口：读 TTML，写出三个 jsonl，返回文件路径字典。"""
    converter = TTMLConverter(input_path, tick_rate=tick_rate)
    return converter.run(out_dir)
