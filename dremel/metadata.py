"""列流元数据：叶路径、最大 r/d 与每一级结构阈值的语义说明。

元数据只描述结构与阈值，绝不携带任何原记录数据、行号或数组下标——
反解只能依赖各列自身的 r/d 与 value。
"""

from __future__ import annotations

import json
from pathlib import Path

from .planner import Layer, LeafPlan

META_FILE = "_meta.json"
RECORD_COUNT_FILE = "_record_count.json"


def _layer_thresholds(layer: Layer) -> list[dict]:
    """生成该层涉及的定义级阈值说明（按 d 升序）。"""
    out: list[dict] = []
    if layer.optional:
        out.append(
            {
                "d": layer.d_before,
                "meaning": f"optional 节点 {layer.name!r} 缺失或为 null，停在进入该层之前",
            }
        )
    if layer.kind == "list":
        out.append(
            {
                "d": layer.d_present,
                "meaning": f"list {layer.name!r} 存在但为空 list，停在进入 repeated element 层之前",
            }
        )
        out.append(
            {
                "d": layer.d_repeat,
                "meaning": f"已进入 {layer.name!r} 的 repeated element 层（r={layer.r_level}）",
            }
        )
        if layer.element_optional:
            out.append(
                {
                    "d": layer.d_element,
                    "meaning": f"list {layer.name!r} 的元素非 null，越过元素自身的 optional 层",
                }
            )
    elif layer.kind == "struct" and layer.optional:
        out.append(
            {
                "d": layer.d_present,
                "meaning": f"optional struct {layer.name!r} 存在（空 struct 也在此级）",
            }
        )
    elif layer.kind == "leaf":
        out.append(
            {
                "d": layer.d_present,
                "meaning": f"叶 {layer.name!r} 到达最大定义级，条目必须携带 value",
            }
        )
    return out


def build_meta(schema: dict, plans: dict[tuple[str, ...], LeafPlan]) -> dict:
    columns = []
    for path, plan in sorted(plans.items()):
        thresholds: list[dict] = []
        for layer in plan.layers:
            thresholds.extend(_layer_thresholds(layer))
        # 合并同 d 说明并按 d 排序
        merged: dict[int, str] = {}
        for item in thresholds:
            merged.setdefault(item["d"], item["meaning"])
        columns.append(
            {
                "leaf_path": list(path),
                "column_file": "__".join(path) + ".jsonl",
                "value_type": plan.layers[-1].value_type,
                "max_repetition_level": plan.max_r,
                "max_definition_level": plan.max_d,
                "repetition_levels": [
                    {"r": r, "meaning": _r_meaning(r)}
                    for r in range(plan.max_r + 1)
                ],
                "definition_thresholds": [
                    {"d": d, "meaning": merged[d]} for d in sorted(merged)
                ],
            }
        )
    return {
        "format": "dremel-like-leaf-column-jsonl/v1",
        "schema": schema,
        "entry_shape": "每行一个 JSON 对象 {r:int, d:int, value?:int|string}；仅当 d 等于该列 max_definition_level 时允许且必须出现 value",
        "columns": columns,
    }


def _r_meaning(r: int) -> str:
    if r == 0:
        return "根记录的第一个条目（在根记录边界重新开始）"
    if r == 1:
        return "同一外层 list 的下一个元素"
    if r == 2:
        return "同一内层 list 的下一个元素"
    return f"第 {r} 层重复"


def write_meta(meta: dict, record_count: int, out_dir: str | Path) -> None:
    out = Path(out_dir)
    (out / META_FILE).write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / RECORD_COUNT_FILE).write_text(
        json.dumps({"record_count": record_count}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_meta(out_dir: str | Path) -> tuple[dict, int]:
    out = Path(out_dir)
    meta = json.loads((out / META_FILE).read_text(encoding="utf-8"))
    count_doc = json.loads((out / RECORD_COUNT_FILE).read_text(encoding="utf-8"))
    if not isinstance(count_doc, dict) or not isinstance(count_doc.get("record_count"), int):
        raise ValueError("记录数文件格式非法")
    return meta, count_doc["record_count"]
