"""作业编排：加载 → 预检编译 → 分批执行 → 汇总输出。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .compiler import Plan, compile_all
from .engine import Engine, Trace
from .loader import load_columns, load_expressions, load_schema


@dataclass
class JobResult:
    plans: dict[str, Plan]
    outputs: dict[str, list[dict]]   # {表达式: [{value, is_null, error}...]}
    row_ids: list[str]
    trace: Trace
    batch_size: int
    n_rows: int

    def compile_plan_dict(self) -> dict:
        return {
            "expressions": [self.plans[name].to_dict() for name in self.plans],
        }

    def output_dict(self) -> dict:
        return {
            "batch_size": self.batch_size,
            "n_rows": self.n_rows,
            "expressions": [
                {
                    "name": name,
                    "result_type": self.plans[name].root.type,
                    "rows": [
                        {"row_id": self.row_ids[i], **rec}
                        for i, rec in enumerate(self.outputs[name])
                    ],
                }
                for name in self.plans
            ],
        }

    def trace_dict(self) -> dict:
        return {
            "batch_size": self.batch_size,
            "expressions": self.trace.to_dict(list(self.plans)),
        }


def run_job(fixtures_dir: Path, batch_size: int) -> JobResult:
    schema = load_schema(fixtures_dir / "schema.json")
    columns, n_rows = load_columns(fixtures_dir / "columns.json", schema)
    expressions = load_expressions(fixtures_dir / "expressions.json")
    plans = compile_all(expressions, schema)   # 预检失败在此整体拒绝

    if "row_id" in columns and all(v is not None for v in columns["row_id"]):
        row_ids = [str(v) for v in columns["row_id"]]
    else:
        row_ids = [str(i) for i in range(n_rows)]

    trace = Trace()
    engine = Engine(plans, columns, schema, n_rows, batch_size, trace)
    outputs = engine.run()
    return JobResult(plans=plans, outputs=outputs, row_ids=row_ids,
                     trace=trace, batch_size=batch_size, n_rows=n_rows)
