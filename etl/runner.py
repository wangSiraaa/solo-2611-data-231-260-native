"""按规划器产出的可执行计划真实运行，并留下完整执行记录。

关键约束：
- 只执行 executable_plan 里的节点；除显式 sort 节点外没有任何排序动作；
- sort 节点调用与 Python 稳定排序一一对应，记录真实输入/输出行数；
- stream_group 按连续组消费，由 ops.stream_groups 再做一次顺序守卫；
- concat 执行期再次核对相邻非空输入边界，防止计划与执行脱节。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .catalog import Catalog
from .errors import OrderingError, PlanError
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
    stable_sort,
    verify_sorted,
)

_OPS = {"source", "filter", "project", "concat", "stream_group", "sort"}


@dataclass
class NodeRecord:
    node_id: str
    op: str
    inputs: list[str]
    input_rows: int
    output_rows: int
    detail: dict[str, Any] = field(default_factory=dict)
    sort_call: dict[str, Any] | None = None


class Runner:
    def __init__(self, catalog: Catalog):
        self.catalog = catalog
        self._materialized: dict[str, list[dict]] = {}
        self._schemas: dict[str, dict[str, str]] = {}
        self.records: list[NodeRecord] = []
        self.sort_calls: list[dict[str, Any]] = []

    def run(self, executable_plan: dict) -> tuple[list[dict], dict]:
        nodes = executable_plan.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise PlanError("可执行计划的 nodes 必须是非空数组")
        raw: dict[str, dict] = {}
        for node in nodes:
            if node.get("id") in raw:
                raise PlanError(f"执行计划节点 id 重复: {node.get('id')!r}")
            if node.get("op") not in _OPS:
                raise PlanError(f"执行计划含未知算子: {node.get('op')!r}")
            raw[node["id"]] = node
        output_id = executable_plan.get("output")
        if output_id not in raw:
            raise PlanError("执行计划的 output 未指向已知节点")
        order = self._topo_order(raw, output_id)

        for node_id in order:
            node = raw[node_id]
            op = node["op"]
            if op == "source":
                self._run_source(node)
            elif op == "filter":
                self._run_filter(node)
            elif op == "project":
                self._run_project(node)
            elif op == "concat":
                self._run_concat(node)
            elif op == "sort":
                self._run_sort(node)
            else:
                self._run_stream_group(node)

        output_rows = self._materialized[output_id]
        report = {
            "plan_id": executable_plan["id"],
            "output": output_id,
            "sort_calls": list(self.sort_calls),
            "sort_call_count": len(self.sort_calls),
            "total_input_rows": sum(
                r.input_rows for r in self.records if r.op == "source"
            ),
            "total_output_rows": len(output_rows),
            "node_records": [self._record_to_dict(r) for r in self.records],
            "output_rows": output_rows,
        }
        return output_rows, report

    # -- 拓扑 ---------------------------------------------------------------

    @staticmethod
    def _refs(node: dict) -> list[str]:
        if node["op"] == "source":
            return []
        if node["op"] == "concat":
            return list(node["inputs"])
        return [node["input"]]

    def _topo_order(self, raw: dict[str, dict], output_id: str) -> list[str]:
        order: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visited:
                return
            if node_id in visiting:
                raise PlanError(f"执行计划存在环，涉及节点 {node_id!r}")
            visiting.add(node_id)
            for ref in self._refs(raw[node_id]):
                if ref not in raw:
                    raise PlanError(f"节点 {node_id!r} 引用未知节点 {ref!r}")
                visit(ref)
            visiting.discard(node_id)
            visited.add(node_id)
            order.append(node_id)

        visit(output_id)
        return order

    # -- 算子执行 ------------------------------------------------------------

    def _run_source(self, node: dict) -> None:
        source_name = node["source"]
        source = self.catalog.source(source_name)
        # 执行期再次真实读取并核验声明，篡改数据会在这里失败。
        rows = self.catalog.load(source_name)
        verify_sorted(rows, source.order_specs, f"source {source_name}")
        self._materialized[node["id"]] = rows
        self._schemas[node["id"]] = dict(source.schema)
        self.records.append(
            NodeRecord(
                node_id=node["id"],
                op="source",
                inputs=[],
                input_rows=0,
                output_rows=len(rows),
                detail={"source": source_name, "declared_order": source.order_specs},
            )
        )

    def _run_filter(self, node: dict) -> None:
        ref = node["input"]
        incoming = self._materialized[ref]
        predicate = build_filter_predicate(node)
        rows = apply_filter(incoming, predicate)
        self._materialized[node["id"]] = rows
        self._schemas[node["id"]] = dict(self._schemas[ref])
        self.records.append(
            NodeRecord(
                node_id=node["id"],
                op="filter",
                inputs=[ref],
                input_rows=len(incoming),
                output_rows=len(rows),
                detail={"predicate": _predicate_text(node)},
            )
        )

    def _run_project(self, node: dict) -> None:
        ref = node["input"]
        incoming = self._materialized[ref]
        rows, schema = apply_project(
            incoming, self._schemas[ref], node["mapping"], node["id"]
        )
        self._materialized[node["id"]] = rows
        self._schemas[node["id"]] = schema
        self.records.append(
            NodeRecord(
                node_id=node["id"],
                op="project",
                inputs=[ref],
                input_rows=len(incoming),
                output_rows=len(rows),
                detail={"mapping": node["mapping"]},
            )
        )

    def _run_concat(self, node: dict) -> None:
        refs = list(node["inputs"])
        parts = [self._materialized[ref] for ref in refs]
        schema = dict(self._schemas[refs[0]])
        for ref in refs[1:]:
            if self._schemas[ref] != schema:
                raise PlanError(f"concat {node['id']!r} 输入 schema 不一致")

        # 执行期边界复核：只有规划器声称 proven 的 concat 才需要核对；
        # unproven 的顺序责任已由下游显式 sort 承担，这里不做越权排序。
        claim = node.get("order_claim") or {}
        common = claim.get("specs") if claim.get("status") == "proven" else None
        boundary_checks: list[dict[str, Any]] = []
        if common:
            previous_ref: str | None = None
            for ref in refs:
                part = self._materialized[ref]
                if not part:
                    continue
                if previous_ref is not None:
                    last_row = self._materialized[previous_ref][-1]
                    first_row = part[0]
                    cmp_result = compare_rows(last_row, first_row, common)
                    boundary_checks.append(
                        {
                            "left": previous_ref,
                            "right": ref,
                            "cmp": cmp_result,
                        }
                    )
                    if cmp_result > 0:
                        raise OrderingError(
                            f"concat {node['id']!r} 执行期边界倒退: "
                            f"{previous_ref} -> {ref}"
                        )
                previous_ref = ref

        rows = concat_rows(parts)
        self._materialized[node["id"]] = rows
        self._schemas[node["id"]] = schema
        self.records.append(
            NodeRecord(
                node_id=node["id"],
                op="concat",
                inputs=refs,
                input_rows=sum(len(part) for part in parts),
                output_rows=len(rows),
                detail={"boundary_checks": boundary_checks},
            )
        )

    def _run_sort(self, node: dict) -> None:
        ref = node["input"]
        incoming = self._materialized[ref]
        specs = node["keys"]
        # 唯一允许发生真实排序的地方：Python 稳定排序。
        rows = stable_sort(incoming, specs)
        verify_sorted(rows, specs, f"sort {node['id']!r}")
        self._materialized[node["id"]] = rows
        self._schemas[node["id"]] = dict(self._schemas[ref])
        sort_call = {
            "sort_node": node["id"],
            "injected_for": node.get("injected_for"),
            "keys": specs,
            "stable": True,
            "input_rows": len(incoming),
            "output_rows": len(rows),
        }
        self.sort_calls.append(sort_call)
        self.records.append(
            NodeRecord(
                node_id=node["id"],
                op="sort",
                inputs=[ref],
                input_rows=len(incoming),
                output_rows=len(rows),
                detail={"keys": specs, "injected_for": node.get("injected_for")},
                sort_call=sort_call,
            )
        )

    def _run_stream_group(self, node: dict) -> None:
        ref = node["input"]
        incoming = self._materialized[ref]
        keys = node["keys"]
        sum_field = node["sum"]
        required = required_group_specs(keys)
        # 执行期守卫：连续组消费的前提必须成立。
        verify_sorted(incoming, required, f"stream_group {node['id']!r} 输入")
        rows = list(stream_groups(incoming, keys, sum_field))
        schema = group_output_schema(keys, self._schemas[ref], sum_field)
        self._materialized[node["id"]] = rows
        self._schemas[node["id"]] = schema
        self.records.append(
            NodeRecord(
                node_id=node["id"],
                op="stream_group",
                inputs=[ref],
                input_rows=len(incoming),
                output_rows=len(rows),
                detail={
                    "keys": keys,
                    "sum": sum_field,
                    "required_prefix": required,
                    "groups": len(rows),
                },
            )
        )

    # -- 辅助 ---------------------------------------------------------------

    @staticmethod
    def _record_to_dict(record: NodeRecord) -> dict[str, Any]:
        return {
            "node_id": record.node_id,
            "op": record.op,
            "inputs": record.inputs,
            "input_rows": record.input_rows,
            "output_rows": record.output_rows,
            "detail": record.detail,
            "sort_call": record.sort_call,
        }


def _predicate_text(node: dict) -> dict[str, Any]:
    for key in ("eq", "ne", "gt", "ge", "lt", "le", "is_null", "not_null"):
        if key in node:
            return {"field": node["field"], key: node[key]}
    return {"field": node.get("field")}


def run_plan(catalog: Catalog, executable_plan: dict) -> tuple[list[dict], dict]:
    runner = Runner(catalog)
    return runner.run(executable_plan)
