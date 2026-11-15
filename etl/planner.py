"""物理顺序可证明性驱动的规划器。

每个节点推导一个 order_specs 描述符（可证明则非空，证明不了就是空列表），
并附带证据或失效原因。只有 stream_group 在输入前缀无法证明时才插入一个
显式 sort 节点；已有充分顺序时绝不无条件再排。

规划期会用小数据做一次“探针”物化来核对 concat 的末/首边界，但不会产生
任何 Sort 调用记录——真实的排序只发生在 runner 执行计划中的 sort 节点上。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import Catalog
from .errors import PlanError
from .ops import (
    apply_filter,
    apply_project,
    build_filter_predicate,
    concat_rows,
    group_output_schema,
    stream_groups,
)
from .ordering import (
    compare_rows,
    required_group_specs,
    same_specs,
    satisfies_prefix,
    stable_sort,
)

_OPS = {"source", "filter", "project", "concat", "stream_group"}
_INTEGER_TYPES = {"int", "int?"}


@dataclass
class NodeState:
    node: dict
    order_specs: list[dict] = field(default_factory=list)
    schema: dict[str, str] = field(default_factory=dict)
    evidence: str = ""
    invalid_reason: str | None = None
    probe_rows: list[dict] = field(default_factory=list)
    input_refs: list[str] = field(default_factory=list)


class Planner:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self.states: dict[str, NodeState] = {}
        self.injected_sorts: list[dict] = []
        self._sort_seq = 0

    # -- 入口 ---------------------------------------------------------------

    def plan(self, plan_decl: dict) -> dict:
        plan_id = plan_decl.get("id")
        nodes = plan_decl.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise PlanError(f"计划 {plan_id!r} 的 nodes 必须是非空数组")
        raw: dict[str, dict] = {}
        for node in nodes:
            if not isinstance(node, dict) or "id" not in node or "op" not in node:
                raise PlanError(f"计划 {plan_id!r} 中存在非法节点: {node!r}")
            if node["id"] in raw:
                raise PlanError(f"计划 {plan_id!r} 节点 id 重复: {node['id']!r}")
            if node["op"] not in _OPS:
                raise PlanError(
                    f"计划 {plan_id!r} 节点 {node['id']!r} 使用不支持的 op {node['op']!r}"
                )
            raw[node["id"]] = node
        output_id = plan_decl.get("output")
        if output_id not in raw:
            raise PlanError(f"计划 {plan_id!r} 的 output 未指向已知节点")
        order = self._topo_order(raw, output_id)

        for node_id in order:
            node = raw[node_id]
            op = node["op"]
            if op == "source":
                self._plan_source(node)
            elif op == "filter":
                self._plan_filter(node)
            elif op == "project":
                self._plan_project(node)
            elif op == "concat":
                self._plan_concat(node)
            else:
                self._plan_stream_group(node)

        return self._build_canonical_plan(plan_decl, raw, order)

    # -- 拓扑 ---------------------------------------------------------------

    @staticmethod
    def _input_refs(node: dict) -> list[str]:
        if node["op"] == "source":
            return []
        if node["op"] == "concat":
            refs = list(node.get("inputs", []))
        else:
            refs = [node["input"]] if "input" in node else []
        if not refs:
            raise PlanError(f"节点 {node['id']!r} 缺少输入声明")
        return refs

    def _topo_order(self, raw: dict[str, dict], output_id: str) -> list[str]:
        order: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str, path: tuple[str, ...]) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise PlanError(f"计划存在环: {' -> '.join(path + (node_id,))}")
            visiting.add(node_id)
            for ref in self._input_refs(raw[node_id]):
                if ref not in raw:
                    raise PlanError(f"节点 {node_id!r} 引用了未知节点 {ref!r}")
                visit(ref, path + (node_id,))
            visiting.discard(node_id)
            visited.add(node_id)
            order.append(node_id)

        visit(output_id, ())
        return order

    # -- 各算子推导 ----------------------------------------------------------

    def _plan_source(self, node: dict) -> None:
        source_name = node.get("source")
        if not isinstance(source_name, str):
            raise PlanError(f"source 节点 {node['id']!r} 缺少 source 名")
        source = self.catalog.source(source_name)
        # 真实读取即核验声明顺序；虚假声明在这里直接失败。
        rows = self.catalog.load(source_name)
        self.states[node["id"]] = NodeState(
            node=node,
            order_specs=[dict(s) for s in source.order_specs],
            schema=dict(source.schema),
            evidence=(
                f"source {source_name} 声明并经真实数据逐相邻行核验: "
                f"{_spec_text(source.order_specs)}"
            ),
            probe_rows=rows,
        )

    def _plan_filter(self, node: dict) -> None:
        child = self._require_child(node)
        # 编译期就拒绝未知谓词。
        predicate = build_filter_predicate(node)
        kept = apply_filter(child.probe_rows, predicate)
        state = NodeState(
            node=node,
            order_specs=[dict(s) for s in child.order_specs],
            schema=dict(child.schema),
            input_refs=[node["input"]],
            probe_rows=kept,
        )
        if child.order_specs:
            state.evidence = (
                f"稳定 filter 不重排，完整继承输入 {node['input']} 的可证明次序 "
                f"{_spec_text(child.order_specs)}（{len(kept)}/{len(child.probe_rows)} 行保留）"
            )
        else:
            state.invalid_reason = (
                f"输入 {node['input']} 本身无可证明次序，filter 不会凭空产生顺序"
            )
        self.states[node["id"]] = state

    def _plan_project(self, node: dict) -> None:
        child = self._require_child(node)
        mapping = node.get("mapping")
        if not isinstance(mapping, dict):
            raise PlanError(f"project 节点 {node['id']!r} 的 mapping 必须是对象")
        projected, output_schema = apply_project(
            child.probe_rows, child.schema, mapping, node["id"]
        )
        source_to_output = {src: out for out, src in mapping.items()}

        derived: list[dict] = []
        dropped: str | None = None
        for spec in child.order_specs:
            source_field = spec["field"]
            if source_field not in source_to_output:
                dropped = source_field
                break
            derived.append(
                {
                    "field": source_to_output[source_field],
                    "direction": spec["direction"],
                    "nulls": spec["nulls"],
                }
            )

        state = NodeState(
            node=node,
            order_specs=derived,
            schema=output_schema,
            input_refs=[node["input"]],
            probe_rows=projected,
        )
        if not child.order_specs:
            state.invalid_reason = (
                f"输入 {node['input']} 无可证明次序，project 不会产生新顺序"
            )
        elif dropped is not None:
            state.invalid_reason = (
                f"前导排序键 {dropped!r} 被 project 删除，其后的列无法再声称全局有序；"
                f"仅保留下游无法依赖的截断描述符 {_spec_text(derived) or '（空）'}"
            )
        else:
            state.evidence = (
                f"全部排序键沿原顺序保留并改名: {_spec_text(derived)}"
            )
        self.states[node["id"]] = state

    def _plan_concat(self, node: dict) -> None:
        refs = list(node.get("inputs", []))
        if not refs:
            raise PlanError(f"concat 节点 {node['id']!r} 的 inputs 不能为空")
        children = [self.states[ref] for ref in refs]
        schema = self._shared_concat_schema(node["id"], children)
        parts = [child.probe_rows for child in children]

        non_empty = [(ref, child) for ref, child in zip(refs, children) if child.probe_rows]
        state = NodeState(
            node=node,
            schema=schema,
            input_refs=refs,
            probe_rows=concat_rows(parts),
        )

        def finish_unproven(reason: str) -> None:
            state.invalid_reason = reason
            state.node = {
                **node,
                "order_claim": {"status": "unproven", "specs": [], "boundary_checks": []},
            }
            self.states[node["id"]] = state

        if not non_empty:
            specs = [dict(s) for s in children[0].order_specs]
            state.order_specs = specs
            state.evidence = "所有输入均为空，空序列在任意键下平凡有序"
            state.node = {
                **node,
                "order_claim": {
                    "status": "proven" if specs else "unproven",
                    "specs": specs,
                    "boundary_checks": [],
                },
            }
            self.states[node["id"]] = state
            return

        first_ref, first_child = non_empty[0]
        common = first_child.order_specs
        if not common:
            finish_unproven(
                f"首个非空输入 {first_ref} 无可证明次序，concat 无法继承任何顺序"
            )
            return
        for ref, child in non_empty[1:]:
            if not child.order_specs:
                finish_unproven(f"输入 {ref} 内部已无可证明次序，concat 后顺序失效")
                return
            if not same_specs(child.order_specs, common):
                finish_unproven(
                    f"不能仅因 order 字段相似就继承：输入 {ref} 的描述符 "
                    f"{_spec_text(child.order_specs)} 与 {first_ref} 的 "
                    f"{_spec_text(common)} 不一致"
                )
                return

        # 内部都有序且描述符一致还不够：核对相邻非空输入的末/首边界。
        boundary_evidence: list[str] = []
        prev_ref, prev_child = non_empty[0]
        for ref, child in non_empty[1:]:
            last_row = prev_child.probe_rows[-1]
            first_row = child.probe_rows[0]
            cmp_result = compare_rows(last_row, first_row, common)
            if cmp_result > 0:
                finish_unproven(
                    f"边界倒退：{prev_ref} 末行 {_key_text(last_row, common)} -> "
                    f"{ref} 首行 {_key_text(first_row, common)} 在 "
                    f"{_spec_text(common)} 下不是非递减"
                )
                return
            boundary_evidence.append(
                f"{prev_ref}末 {_key_text(last_row, common)} <= "
                f"{ref}首 {_key_text(first_row, common)}"
            )
            prev_ref, prev_child = ref, child

        state.order_specs = [dict(s) for s in common]
        state.evidence = (
            f"各非空输入描述符一致且边界非递减（{'; '.join(boundary_evidence)}），"
            f"concat 全局保持 {_spec_text(common)}"
        )
        state.node = {
            **node,
            "order_claim": {
                "status": "proven",
                "specs": [dict(s) for s in common],
                "boundary_checks": boundary_evidence,
            },
        }
        self.states[node["id"]] = state

    def _plan_stream_group(self, node: dict) -> None:
        child = self._require_child(node)
        keys = node.get("keys")
        sum_field = node.get("sum")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
            raise PlanError(f"stream_group 节点 {node['id']!r} 的 keys 必须是非空字符串数组")
        if len(set(keys)) != len(keys):
            raise PlanError(f"stream_group 节点 {node['id']!r} 的 keys 不能重复")
        if not isinstance(sum_field, str):
            raise PlanError(f"stream_group 节点 {node['id']!r} 缺少整数列 sum")
        if sum_field not in child.schema:
            raise PlanError(
                f"stream_group 节点 {node['id']!r} 的 sum 列 {sum_field!r} 不在输入 schema 中"
            )
        if child.schema[sum_field] not in _INTEGER_TYPES:
            raise PlanError(
                f"stream_group 节点 {node['id']!r} 的 sum 列 {sum_field} "
                f"必须是整数列，实际 {child.schema[sum_field]}"
            )
        for key in keys:
            if key not in child.schema:
                raise PlanError(
                    f"stream_group 节点 {node['id']!r} 的键 {key!r} 不在输入 schema 中"
                )

        required = required_group_specs(keys)
        ok, reason = satisfies_prefix(child.order_specs, required)
        effective_input = node["input"]
        sort_node_id: str | None = None

        if ok:
            evidence = (
                f"输入 {node['input']} 的可证明次序满足分组前缀 "
                f"{_spec_text(required)}：{reason}，无需插入 Sort"
            )
            group_input_rows = child.probe_rows
        else:
            sort_node_id = self._inject_sort(node, required, reason)
            effective_input = sort_node_id
            evidence = (
                f"输入 {node['input']} 无法证明分组前缀 {_spec_text(required)}（{reason}），"
                f"已插入显式 Sort {sort_node_id}"
            )
            # 探针排序仅用于推导，不产生执行记录；真实排序由 runner 完成。
            group_input_rows = stable_sort(child.probe_rows, required)

        output_rows = list(stream_groups(group_input_rows, keys, sum_field))
        output_schema = group_output_schema(keys, child.schema, sum_field)
        state = NodeState(
            node=node,
            order_specs=[dict(s) for s in required],
            schema=output_schema,
            input_refs=[effective_input],
            probe_rows=output_rows,
            evidence=evidence,
        )
        self.states[node["id"]] = state

    # -- 辅助 ---------------------------------------------------------------

    def _require_child(self, node: dict) -> NodeState:
        ref = node.get("input")
        if not isinstance(ref, str) or ref not in self.states:
            raise PlanError(f"节点 {node['id']!r} 的 input 未指向已知上游节点")
        return self.states[ref]

    @staticmethod
    def _shared_concat_schema(node_id: str, children: list[NodeState]) -> dict[str, str]:
        base = children[0].schema
        for child in children[1:]:
            if child.schema != base:
                raise PlanError(
                    f"concat 节点 {node_id!r} 各输入 schema 不一致: "
                    f"{base} != {child.schema}"
                )
        return dict(base)

    def _inject_sort(self, parent: dict, required: list[dict], reason: str) -> str:
        self._sort_seq += 1
        sort_id = f"_sort_{self._sort_seq}_{parent['id']}"
        sort_node = {
            "id": sort_id,
            "op": "sort",
            "input": parent["input"],
            "keys": [dict(s) for s in required],
            "stable": True,
            "injected_for": parent["id"],
            "reason": reason,
        }
        self.injected_sorts.append(sort_node)
        return sort_id

    def _build_canonical_plan(
        self, plan_decl: dict, raw: dict[str, dict], order: list[str]
    ) -> dict:
        node_reports: list[dict] = []
        for node_id in order:
            state = self.states[node_id]
            node_reports.append(
                {
                    "id": node_id,
                    "op": state.node["op"],
                    "inputs": state.input_refs,
                    "derived_order": state.order_specs,
                    "schema": state.schema,
                    "evidence": state.evidence,
                    "invalid_reason": state.invalid_reason,
                    "detail": _node_detail(state.node),
                }
            )
        sort_reports = [
            {
                "id": sort_node["id"],
                "input": sort_node["input"],
                "keys": sort_node["keys"],
                "injected_for": sort_node["injected_for"],
                "reason": sort_node["reason"],
            }
            for sort_node in self.injected_sorts
        ]
        # 执行树：原始节点 + 注入 sort，引用关系已在 stream_group 状态里改写。
        # derived_order 是随节点携带的顺序元数据，供执行期边界复核使用，
        # 本身不触发任何排序动作。
        executable_nodes: list[dict] = []
        for node_id in order:
            state = self.states[node_id]
            if state.node["op"] == "stream_group":
                ref = state.input_refs[0]
                if ref.startswith("_sort_"):
                    sort_node = next(s for s in self.injected_sorts if s["id"] == ref)
                    executable_nodes.append(
                        {**sort_node, "derived_order": [dict(k) for k in sort_node["keys"]]}
                    )
                executable_nodes.append(
                    {
                        **state.node,
                        "input": state.input_refs[0],
                        "derived_order": state.order_specs,
                    }
                )
            else:
                executable_nodes.append(
                    {**state.node, "derived_order": state.order_specs}
                )
        return {
            "plan_id": plan_decl["id"],
            "output": plan_decl["output"],
            "sort_count": len(self.injected_sorts),
            "injected_sorts": sort_reports,
            "nodes": node_reports,
            "executable_plan": {
                "id": plan_decl["id"],
                "output": plan_decl["output"],
                "nodes": executable_nodes,
            },
        }


def _spec_text(specs: list[dict]) -> str:
    if not specs:
        return "[]"
    return "[" + ", ".join(
        f"{s['field']} {s['direction']} NULLS {s['nulls'].upper()}" for s in specs
    ) + "]"


def _key_text(row: dict, specs: list[dict]) -> str:
    return "(" + ", ".join(f"{s['field']}={row.get(s['field'])!r}" for s in specs) + ")"


def _node_detail(node: dict) -> dict:
    keys_to_keep = {
        "source", "field", "eq", "ne", "gt", "ge", "lt", "le",
        "is_null", "not_null", "mapping", "keys", "sum",
    }
    return {k: v for k, v in node.items() if k in keys_to_keep}


def load_plans(plans_path: str | Path) -> list[dict]:
    with Path(plans_path).open("r", encoding="utf-8") as handle:
        declarations = json.load(handle)
    if not isinstance(declarations, list):
        raise PlanError("plans.json 顶层必须是数组")
    return declarations


def plan_all(
    sources_path: str | Path, plans_path: str | Path
) -> tuple[Catalog, list[dict]]:
    catalog = Catalog(sources_path)
    plans = [Planner(catalog).plan(decl) for decl in load_plans(plans_path)]
    return catalog, plans
