#!/usr/bin/env python3
"""自动验证：独立于转换器实现，用手工时间表与独立重算核对输出。

本脚本 *不* 调用转换器内部的时间/层级算法，而是：
  1. 自带一份独立的时间表达式解析（Decimal，s/ms/t）；
  2. 自带一份独立的 par/seq 层级时钟评估器，从原 XML 重新推导区间；
  3. 内置对每个小样例的手工绝对区间表（人工按 timing.md 推导）；
  4. 对每个相邻边界的 *中点* 独立判断可见源节点并拼出 cue 文本，
     再与 cues.jsonl 比较——而不是把转换器结果再序列化一遍；
  5. 回查原 XML 验证 source_nodes 的路径与字符原文；
  6. 检查 p1/p2 重叠、seq 间隙、父裁切、clipped_out、bad-time 拒绝。
运行：python verify.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ttml2cues.converter import merge_adjacent_fragments  # 纯函数，仅做单元测试  # noqa: E402
from ttml2cues.timing import TICK_RATE  # noqa: E402

NS = {
    "tt": "http://www.w3.org/ns/ttml",
    "ttp": "http://www.w3.org/ns/ttml#parameter",
    "xml": "http://www.w3.org/XML/1998/namespace",
}

# --------------------------------------------------------------------- #
# 独立时间解析（与 ttml2cues.timing 无代码共享）
# --------------------------------------------------------------------- #
_DECIMAL_RE = re.compile(r"^(\d+(?:\.\d+)?)(s|ms)$")
_TICK_RE = re.compile(r"^\d+t$")


def independent_ticks(expr: str) -> int:
    text = expr.strip()
    if _TICK_RE.fullmatch(text):
        return int(text[:-1])
    m = _DECIMAL_RE.fullmatch(text)
    assert m, f"独立解析器不认识表达式 {expr!r}"
    value = Decimal(m.group(1))
    factor = Decimal(TICK_RATE) if m.group(2) == "s" else Decimal(1)
    ticks = value * factor
    assert ticks == ticks.to_integral_value(), f"{expr} 不是整数 tick"
    return int(ticks)


# --------------------------------------------------------------------- #
# 独立层级时钟评估器（只服务于验证，重写一遍而不是 import）
# --------------------------------------------------------------------- #
class VNode:
    def __init__(self, el: ET.Element, parent: "VNode | None", index: int):
        self.el = el
        self.local = el.tag.split("}", 1)[1]
        self.parent = parent
        self.index = index
        self.xml_id = el.get(f"{{{NS['xml']}}}id")
        tc = el.get("timeContainer", "par")
        assert tc in ("par", "seq")
        self.tc = tc
        self.us = self.ue = None       # 未裁切
        self.es = self.ee = None       # 有效（裁切后）

    @property
    def key(self) -> str:
        return self.xml_id or f"/{self.local}[{self.index}]"


def build_tree(root_el: ET.Element) -> dict[str, VNode]:
    body = [c for c in root_el if c.tag.endswith("}body")][0]
    nodes: dict[str, VNode] = {}

    def walk(el: ET.Element, parent: VNode | None, index: int) -> VNode:
        node = VNode(el, parent, index)
        nodes[node.key] = node
        i = 0
        for child in el:
            if not isinstance(child.tag, str):
                continue
            i += 1
            if child.tag.endswith("}br"):
                continue  # br 不参与定时
            walk(child, node, i)
        return node

    walk(body, None, 1)
    return nodes


def evaluate(nodes: dict[str, VNode]) -> None:
    body = next(n for n in nodes.values() if n.local == "body")

    def resolve(node: VNode, base: int, kind: str, ancestors: list[tuple[int, int]]) -> None:
        begin = independent_ticks(node.el.attrib["begin"]) if "begin" in node.el.attrib else 0
        start = base + begin
        ends: list[int] = []
        if "end" in node.el.attrib:
            ends.append(base + independent_ticks(node.el.attrib["end"]))
        if "dur" in node.el.attrib:
            ends.append(start + independent_ticks(node.el.attrib["dur"]))
        if not ends:
            assert node.parent is not None and node.parent.tc == "par"
            end = node.parent.ue
        else:
            end = min(ends)
        assert end >= start, f"{node.key}: end<start"
        node.us, node.ue = start, end
        lo, hi = start, end
        for a0, a1 in ancestors:
            lo, hi = max(lo, a0), min(hi, a1)
        if lo < hi:
            node.es, node.ee = lo, hi
        child_anc = ancestors + ([(node.es, node.ee)] if node.es is not None else [])
        children = [
            n for n in nodes.values() if n.parent is node
        ]
        if node.tc == "seq":
            prev = node.us
            for child in children:
                resolve(child, prev, "seq", child_anc)
                prev = child.ue  # 未裁切结束推进，与裁切无关
        else:
            for child in children:
                resolve(child, node.us, "par", child_anc)

    base0 = 0
    body_dur = independent_ticks(body.el.attrib["dur"])
    body.us, body.ue = base0, base0 + body_dur
    body.es, body.ee = body.us, body.ue
    for child in [n for n in nodes.values() if n.parent is body]:
        resolve(child, body.us, "par", [(body.es, body.ee)])


# --------------------------------------------------------------------- #
# 手工绝对区间表（人工推导，不来自转换器）
# --------------------------------------------------------------------- #
CAPTIONS_HAND_TABLE = {
    # p_id: (未裁切 start, 未裁切 end, 有效 start, 有效 end)
    "p1": (1500, 5500, 1500, 5500),
    "p2": (3000, 9000, 3000, 7000),   # end=8s 相对 par 基准 1000 -> 9000；父裁到 7000
    "p3": (8000, 9000, 8000, 9000),
    "p4": (9250, 10750, 9250, 10750),  # seq 间隙 9000->9250
    "p5": (11250, 13250, 11250, 12000),  # 父 body 12000 裁切，未裁切保留 13250
}

# 人工枚举的源节点有效时界（同样独立推导）
CAPTIONS_SOURCE_HAND = {
    "p1#text-1": (1500, 5500, "Hello "),
    "s1#text-2": (2500, 4000, "world"),
    "s1#tail-3": (1500, 5500, "!"),
    "s2#text-1": (3000, 5000, "甲"),
    "s3#text-2": (4000, 7000, "乙"),
    "p3#text-1": (8000, 9000, "first"),
    "p4#text-1": (9250, 10750, "second"),
    "p5#text-1": (11250, 12000, "clipped"),
}

# 人工枚举的期望 cue（p_id, start, end, text），顺序按 p 文档序 + 时间序
CAPTIONS_CUES_HAND = [
    ("p1", 1500, 2500, "Hello !"),
    ("p1", 2500, 4000, "Hello world!"),
    ("p1", 4000, 5500, "Hello !"),
    ("p2", 3000, 4000, "甲"),
    ("p2", 4000, 5000, "甲乙"),
    ("p2", 5000, 7000, "乙"),
    ("p3", 8000, 9000, "first"),
    ("p4", 9250, 10750, "second"),
    ("p5", 11250, 12000, "clipped"),
]

# 手工源节点 -> 所属 p（span 内节点归属于包含它的 p）
CAPTIONS_SOURCE_OWNER_P = {
    "p1#text-1": "p1",
    "s1#text-2": "p1",
    "s1#tail-3": "p1",
    "s2#text-1": "p2",
    "s3#text-2": "p2",
    "p3#text-1": "p3",
    "p4#text-1": "p4",
    "p5#text-1": "p5",
}

EMPTY_HAND_TABLE = {
    "empty": (2000, 3000, None, None),  # body 0..1000 内完全裁空
}


# --------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------- #
def read_jsonl(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def run_converter(input_path: Path, out_dir: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ttml2cues", str(input_path), "-o", str(out_dir)],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )


def check(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)
    print(f"  ok - {message}")


# --------------------------------------------------------------------- #
# 各项验证
# --------------------------------------------------------------------- #
def verify_captions(tmp: Path) -> None:
    print("[1] captions.ttml：手工区间表 + 独立层级评估器 + 中点可见性")
    out = tmp / "captions"
    proc = run_converter(ROOT / "fixtures" / "captions.ttml", out)
    check(proc.returncode == 0, "正常材料转换退出码为 0")

    paragraphs = {r["p_id"]: r for r in read_jsonl(out / "paragraphs.jsonl")}
    cues = read_jsonl(out / "cues.jsonl")
    sources = {r["source_node_id"]: r for r in read_jsonl(out / "source_nodes.jsonl")}

    # 1a. 与手工绝对区间表逐字段核对
    for p_id, (us, ue, es, ee) in CAPTIONS_HAND_TABLE.items():
        row = paragraphs[p_id]
        check(
            (row["unclipped_start_tick"], row["unclipped_end_tick"]) == (us, ue),
            f"{p_id} 未裁切区间 == 手工表 ({us},{ue})，实际 "
            f"({row['unclipped_start_tick']},{row['unclipped_end_tick']})",
        )
        check(
            (row["effective_start_tick"], row["effective_end_tick"]) == (es, ee),
            f"{p_id} 有效区间 == 手工表 ({es},{ee})",
        )

    # 1b. 独立评估器重算，再与输出比对（双路独立）
    tree = ET.parse(ROOT / "fixtures" / "captions.ttml")
    nodes = build_tree(tree.getroot())
    evaluate(nodes)
    for p_id, (us, ue, es, ee) in CAPTIONS_HAND_TABLE.items():
        node = nodes[p_id]
        check(
            (node.us, node.ue, node.es, node.ee) == (us, ue, es, ee),
            f"独立评估器对 {p_id} 重算 {node.us,node.ue,node.es,node.ee} 与手工表一致",
        )
        row = paragraphs[p_id]
        check(
            (row["unclipped_start_tick"], row["unclipped_end_tick"],
             row["effective_start_tick"], row["effective_end_tick"])
            == (node.us, node.ue, node.es, node.ee),
            f"{p_id} 转换器输出与独立评估器重算一致",
        )

    # 1c. 独立枚举源节点时界 + 手工表，与 source_nodes.jsonl 核对
    for sid, (hs, he, htext) in CAPTIONS_SOURCE_HAND.items():
        row = sources[sid]
        check(
            (row["effective_start_tick"], row["effective_end_tick"], row["text"])
            == (hs, he, htext),
            f"源节点 {sid} 时界/原文 == 手工表 ({hs},{he},{htext!r})",
        )

    # 1d. 回查原 XML：source_node 的字符原文与 XML 路径必须在原文档中真实存在
    raw = (ROOT / "fixtures" / "captions.ttml").read_text(encoding="utf-8")
    for sid, row in sources.items():
        check(row["text"] in raw, f"{sid} 的原文 {row['text']!r} 能在原 XML 中回查")
        check(row["xml_path"].startswith("/body[1]/"), f"{sid} 含完整 XML 路径")

    # 1e. 边界中点独立判可见：对每个 cue 取其中点，用手工源节点表独立判定
    hand_sources = {
        sid: (hs, he, htext) for sid, (hs, he, htext) in CAPTIONS_SOURCE_HAND.items()
    }
    hand_by_p: dict[str, list[str]] = {}
    for sid, owner_p in CAPTIONS_SOURCE_OWNER_P.items():
        hand_by_p.setdefault(owner_p, []).append(sid)

    check(len(cues) == len(CAPTIONS_CUES_HAND), f"cue 数量 {len(cues)} == 手工 {len(CAPTIONS_CUES_HAND)}")
    for cue, (hp, hs, he, htext) in zip(cues, CAPTIONS_CUES_HAND):
        mid = (cue["start_tick"] + cue["end_tick"]) // 2
        # 独立判定：中点落在哪些手工源节点区间里（文档序 = 手工表插入序）
        visible = [
            sid
            for sid in hand_by_p[hp]
            if hand_sources[sid][0] <= mid < hand_sources[sid][1]
        ]
        expected_text = "".join(hand_sources[sid][2] for sid in visible)
        check(cue["p_id"] == hp, f"cue@tick={mid} 属于 {hp}")
        check(
            (cue["start_tick"], cue["end_tick"]) == (hs, he),
            f"cue 边界 ({hs},{he}) 与手工期望一致（中点 {mid}）",
        )
        check(expected_text == htext, f"中点 {mid} 独立拼出的文本 == 手工文本 {htext!r}")
        check(cue["text"] == expected_text, f"cue 文本与中点独立判定一致：{cue['text']!r}")
        check(
            cue["source_node_ids"] == visible,
            f"cue 源序列与中点可见节点集合一致：{cue['source_node_ids']}",
        )

    # 1f. cue 边界必须落在其 p 的有效区间内且片段相邻无重叠无空洞
    for p_id in CAPTIONS_HAND_TABLE:
        p_cues = [c for c in cues if c["p_id"] == p_id]
        _, _, es, ee = CAPTIONS_HAND_TABLE[p_id]
        check(p_cues[0]["start_tick"] == es and p_cues[-1]["end_tick"] == ee,
              f"{p_id} cue 链覆盖整个有效区间 ({es},{ee})")
        for a, b in zip(p_cues, p_cues[1:]):
            check(a["end_tick"] == b["start_tick"], f"{p_id} cue 严格相邻无空洞/重叠")

    # 1g. 不同 p 允许重叠：t=4500 时 p1 与 p2 都应可见，两条 cue 都保留
    overlap = [c for c in cues if c["start_tick"] <= 4500 < c["end_tick"]]
    pids = {c["p_id"] for c in overlap}
    check(pids == {"p1", "p2"}, f"t=4500 重叠保留两条 p（实际 {pids}）")
    overlap_text = {c["p_id"]: c["text"] for c in overlap}
    check(
        overlap_text == {"p1": "Hello !", "p2": "甲乙"},
        f"重叠处两条 cue 文本各自独立（实际 {overlap_text}）",
    )

    # 1h. seq 间隙：p3 结束 9000 与 p4 开始 9250 之间无任何 cue
    gap = [c for c in cues if 9000 <= c["start_tick"] < 9250]
    check(gap == [], "seq 间隙 9000..9250 没有 cue 被凭空生成")

    # 1i. 父裁切：p2 未裁切到 9000 但有效止于 7000；p5 未裁切 13250 但止于 12000
    check(paragraphs["p2"]["effective_end_tick"] == 7000
          and paragraphs["p2"]["unclipped_end_tick"] == 9000,
          "p2 父容器裁切（9000 -> 7000）且未裁切时钟保留")
    check(paragraphs["p5"]["effective_end_tick"] == 12000
          and paragraphs["p5"]["unclipped_end_tick"] == 13250,
          "p5 被 body 裁到 12000，seq 推进用的未裁切 13250 仍记录")

    # 1j. source_node_ids 全部可回查且文档序递增
    all_sids = list(sources)
    for cue in cues:
        for sid in cue["source_node_ids"]:
            check(sid in sources, f"cue 引用的 {sid} 在 source_nodes.jsonl 可回查")
        positions = [all_sids.index(sid) for sid in cue["source_node_ids"]]
        check(positions == sorted(positions), f"cue {cue['cue_id']} 源节点按文档序排列")

    # 1k. 常驻 tail 文本：s1 结束后 "!" 仍按父 p1 时段可见（首末片段都带 tail）
    check(cues[0]["source_node_ids"][-1] == "s1#tail-3", "span 结束后 tail 仍在 p 时段可见")
    check(cues[2]["source_node_ids"][-1] == "s1#tail-3", "tail 在 span 结束后依然参与拼接")


def verify_empty_clipped(tmp: Path) -> None:
    print("[2] empty-clipped.ttml：合法不可见段 + clipped_out 诊断")
    out = tmp / "empty"
    proc = run_converter(ROOT / "fixtures" / "empty-clipped.ttml", out)
    check(proc.returncode == 0, "完全裁空是合法材料，退出码 0")
    paragraphs = read_jsonl(out / "paragraphs.jsonl")
    cues = read_jsonl(out / "cues.jsonl")
    sources = read_jsonl(out / "source_nodes.jsonl")
    row = paragraphs[0]
    us, ue, es, ee = EMPTY_HAND_TABLE["empty"]
    check(
        (row["unclipped_start_tick"], row["unclipped_end_tick"]) == (us, ue),
        "裁空段未裁切区间仍按相对时钟记录 (2000,3000)",
    )
    check(
        (row["effective_start_tick"], row["effective_end_tick"]) == (es, ee),
        "有效区间为空 (null,null)",
    )
    check(row["clipped_out"] is True and row["visible"] is False, "clipped_out=true, visible=false")
    check(cues == [], "不可见段不产生 cue（cues.jsonl 为空）")
    check(len(sources) == 1 and sources[0]["text"] == "outside",
          "源节点诊断保留，原文 outside 可回查，时界为 null")
    check(sources[0]["effective_start_tick"] is None
          and sources[0]["effective_end_tick"] is None,
          "裁空源节点时界为 null")

    # 独立评估器交叉验证
    tree = ET.parse(ROOT / "fixtures" / "empty-clipped.ttml")
    nodes = build_tree(tree.getroot())
    evaluate(nodes)
    empty = nodes["empty"]
    check((empty.us, empty.ue, empty.es, empty.ee) == (2000, 3000, None, None),
          "独立评估器重算 empty 段：未裁切 (2000,3000)，有效为空")


def verify_bad_time(tmp: Path) -> None:
    print("[3] bad-time.ttml：非整数 tick 必须明确失败")
    out = tmp / "bad"
    proc = run_converter(ROOT / "fixtures" / "bad-time.ttml", out)
    check(proc.returncode != 0, f"退出码非 0（实际 {proc.returncode}）")
    check("不是整数 tick" in proc.stderr or "不支持" in proc.stderr,
          f"stderr 给出明确原因：{proc.stderr.strip()}")
    check(not out.exists() or not (out / "cues.jsonl").exists(),
          "失败时不产生半成品输出")

    # 独立解析器自己也判定 0.0001s 非整数 tick
    val = Decimal("0.0001") * Decimal(TICK_RATE)
    check(val != val.to_integral_value(), "独立 Decimal 核算确认 0.0001s = 0.1 tick 非整数")


def verify_merge_unit() -> None:
    print("[4] 合并规则单元测试：仅文本+源序列都相同才合并")
    frags = [
        {"start_tick": 0, "end_tick": 10, "text": "a", "source_node_ids": ["x#text-1"]},
        {"start_tick": 10, "end_tick": 20, "text": "a", "source_node_ids": ["x#text-1"]},
        {"start_tick": 20, "end_tick": 30, "text": "ab", "source_node_ids": ["x#text-1", "y#text-2"]},
        {"start_tick": 30, "end_tick": 40, "text": "a", "source_node_ids": ["z#text-3"]},
        {"start_tick": 40, "end_tick": 50, "text": "a", "source_node_ids": ["z#text-3"]},
    ]
    merged = merge_adjacent_fragments(frags)
    check([(m["start_tick"], m["end_tick"], m["text"]) for m in merged]
          == [(0, 20, "a"), (20, 30, "ab"), (30, 50, "a")],
          "相同文本+相同源序列相邻合并为最大片段；文本或源序列不同则不合并")
    # 不相邻（有间隔）即使相同也不合并
    gap = [
        {"start_tick": 0, "end_tick": 10, "text": "a", "source_node_ids": ["x"]},
        {"start_tick": 20, "end_tick": 30, "text": "a", "source_node_ids": ["x"]},
    ]
    check(len(merge_adjacent_fragments(gap)) == 2, "不相邻片段不合并")
    check(merge_adjacent_fragments([]) == [], "空输入安全")


def verify_synthetic(tmp: Path) -> None:
    print("[5] 合成材料：br 换行 + 常驻文本 + 嵌套 span 的中点独立核对")
    ttml = """<tt xmlns="http://www.w3.org/ns/ttml" xmlns:ttp="http://www.w3.org/ns/ttml#parameter" ttp:tickRate="1000"><body dur="5s"><div timeContainer="par"><p xml:id="q1" begin="1s" dur="3s" xml:space="preserve">A<br/><span xml:id="q1s" begin="1s" dur="1s">B</span>C</p></div></body></tt>"""
    path = tmp / "synth.ttml"
    path.write_text(ttml, encoding="utf-8")
    out = tmp / "synth-out"
    proc = run_converter(path, out)
    check(proc.returncode == 0, f"合成材料转换成功：{proc.stderr}")
    cues = read_jsonl(out / "cues.jsonl")
    sources = {r["source_node_id"]: r for r in read_jsonl(out / "source_nodes.jsonl")}

    # 手工时间表（body 0..5000；p 未裁切=有效 1000..4000；
    # br 与常驻文本全程；span q1s 2000..3000）
    expected = [
        (1000, 2000, "A\nC"),
        (2000, 3000, "A\nBC"),
        (3000, 4000, "A\nC"),
    ]
    check([(c["start_tick"], c["end_tick"], c["text"]) for c in cues] == expected,
          f"br 固定换行、span 外常驻文本全程可见：{[c['text'] for c in cues]}")
    br_nodes = [r for r in sources.values() if r["kind"] == "br"]
    check(len(br_nodes) == 1 and br_nodes[0]["text"] == "\n",
          "br 作为源节点记录且原文为换行符")
    check((br_nodes[0]["effective_start_tick"], br_nodes[0]["effective_end_tick"])
          == (1000, 4000), "br 可见时段继承直接父 p")

    # 中点独立判定再核一遍
    for start, end, text in expected:
        mid = (start + end) // 2
        visible_text = "".join(
            r["text"]
            for r in sources.values()
            if r["effective_start_tick"] is not None
            and r["effective_start_tick"] <= mid < r["effective_end_tick"]
        )
        check(visible_text == text, f"合成材料中点 {mid} 独立判定文本 {text!r}")


def verify_parser_rejections() -> None:
    print("[6] 时间表达式拒绝矩阵（独立 Decimal 口径）")
    for bad in ["0.0001s", "01:02:03", "10f", "1.5t", "", "wallclock(now)"]:
        try:
            independent_ticks(bad)
        except (AssertionError, ValueError):
            print(f"  ok - 独立解析拒绝 {bad!r}")
        else:
            raise AssertionError(f"{bad} 应被拒绝")
    for expr, want in [("1s", 1000), ("0.25s", 250), ("250ms", 250), ("7t", 7), ("0.000ms", 0)]:
        check(independent_ticks(expr) == want, f"{expr} 精确换算是 {want} tick")


def main() -> int:
    if shutil.which(sys.executable) is None:
        print("找不到 python", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="ttml-verify-") as tmp_name:
        tmp = Path(tmp_name)
        verify_captions(tmp)
        verify_empty_clipped(tmp)
        verify_bad_time(tmp)
        verify_merge_unit()
        verify_synthetic(tmp)
        verify_parser_rejections()
    print("\n全部验证通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
