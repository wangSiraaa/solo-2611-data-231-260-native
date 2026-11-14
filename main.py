"""入口：prepare 建库 → 逐查询 plan/run → 与独立 fact 直算对账 → 验证项 → report.json。"""
import json
import sqlite3
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

import rollup

ROOT = Path(__file__).parent

# 新增查询（不在 fixtures/queries.json 中）：证明计划逻辑不按 query_id 硬编码，
# 同时覆盖“允许值集合中的 null 匹配 NULL 维度组”。
EXTRA_QUERY = {
    "id": "extra_null_region",
    "group_by": ["day"],
    "where": {"region": [None]},
    "measures": ["count", "amount_sum", "amount_avg"],
}


def entry_json(e):
    out = {}
    for k, v in e.items():
        if isinstance(v, Fraction):
            # AVG 输出分子/分母（精确），decimal6 为声明 6 位精度的小数
            out[k] = {"num": v.numerator, "den": v.denominator,
                      "decimal6": round(v.numerator / v.denominator, 6)}
        else:
            out[k] = v
    return out


def plan_json(p: rollup.Plan):
    return {
        "candidates": [
            {"name": v.name, "usable": v.usable, "reason": v.reason,
             **({"row_count": v.row_count} if v.row_count is not None else {})}
            for v in p.verdicts
        ],
        "chosen": p.chosen,
        "fallback": p.fallback,
        "fallback_reason": p.fallback_reason,
        "residual_where": p.residual_where,
        "reaggregation": p.reaggregation,
        "sql": p.sql,
        "params": p.params,
    }


def run_all(conn, model, queries):
    results = []
    for q in queries:
        p = rollup.plan(model, q, conn)
        entries, run_tables = rollup.run(conn, model, q, p)
        direct, direct_tables = rollup.direct_fact(conn, model, q)
        ok, diffs = rollup.reconcile(entries, direct, q)
        results.append({
            "query_id": q["id"],
            "plan": plan_json(p),
            "result": [entry_json(e) for e in entries],
            "tables_read": sorted(run_tables),
            "reconcile": {"ok": ok, "diffs": diffs,
                          "direct_fact_tables": sorted(direct_tables)},
        })
    return results


def verify(model, conn, results):
    """显式验证任务要求的关键行为，返回检查项列表。"""
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    by_id = {r["query_id"]: r for r in results}

    # 1) 每个查询中：v_avg（avg-only 无权重）与 v_stale（旧 snapshot）均被拒
    for r in results:
        cands = {c["name"]: c for c in r["plan"]["candidates"]}
        check(f"{r['query_id']}: v_avg 被拒（avg-only 无权重）",
              not cands["v_avg"]["usable"] and "权重" in cands["v_avg"]["reason"],
              cands["v_avg"]["reason"])
        check(f"{r['query_id']}: v_stale 被拒（旧 snapshot）",
              not cands["v_stale"]["usable"] and "snapshot" in cands["v_stale"]["reason"],
              cands["v_stale"]["reason"])

    # 2) 错误过滤域：v_a（只含 product=A）不能回答全商品或 B 商品
    for qid in ("region_avg", "b_only", "day_filter", "global"):
        cands = {c["name"]: c for c in by_id[qid]["plan"]["candidates"]}
        check(f"{qid}: v_a 被拒（过滤域不覆盖）",
              not cands["v_a"]["usable"] and "行域" in cands["v_a"]["reason"]
              or qid == "region_avg" and "预过滤" in cands["v_a"]["reason"],
              cands["v_a"]["reason"])

    # 3) a_only：product=A 已由 v_a 谓词完全满足 → 选 v_a 且无残余过滤
    ra = by_id["a_only"]
    check("a_only: 选用 v_a 且残余过滤为空",
          ra["plan"]["chosen"] == "v_a" and ra["plan"]["residual_where"] == {},
          f"chosen={ra['plan']['chosen']} residual={ra['plan']['residual_where']}")

    # 4) 加权 AVG：region_avg 中 N 区 = 170/5=34，而非子组平均的简单平均
    n_group = next(e for e in by_id["region_avg"]["result"]
                   if e.get("region") == "N")
    avg = n_group["amount_avg"]
    naive = (Fraction(110, 3) + Fraction(60, 2)) / 2  # 按组数平均的错误口径
    got = Fraction(avg["num"], avg["den"])
    check("region_avg: N 区 AVG 按有效行数加权 = 170/5",
          got == Fraction(170, 5) and got != naive,
          f"avg={avg['num']}/{avg['den']}={float(got)}，按组数平均错误值={float(naive):.4f}")

    # 5) NULL 维度作为独立组，且全 NULL 金额组 sum/avg 为 NULL
    null_group = next(e for e in by_id["region_avg"]["result"]
                      if e.get("region") is None)
    check("region_avg: NULL region 独立成组，sum/avg 为 NULL",
          null_group["count"] == 1 and null_group["amount_sum"] is None
          and null_group["amount_avg"] is None, json.dumps(null_group))

    # 6) 无 group_by 的空结果仍输出一行 count=0、sum/avg=NULL
    g = by_id["global"]["result"]
    check("global: 空结果输出一行 count=0、sum/avg=NULL",
          len(g) == 1 and g[0]["count"] == 0 and g[0]["amount_sum"] is None
          and g[0]["amount_avg"] is None, json.dumps(g))

    # 7) 实际读取表：region_avg 用 v_daily 而非 fact；新增查询走 v_daily
    check("region_avg: 实际读取 v_daily（未读 fact）",
          by_id["region_avg"]["tables_read"] == ["v_daily"],
          str(by_id["region_avg"]["tables_read"]))
    check("extra_null_region: 实际读取 v_daily（未读 fact）",
          by_id["extra_null_region"]["tables_read"] == ["v_daily"],
          str(by_id["extra_null_region"]["tables_read"]))

    # 8) 新增查询结果正确（非 query_id 硬编码）：仅 2026-01-02 一行，金额全 NULL
    ex = by_id["extra_null_region"]["result"]
    check("extra_null_region: NULL region 组 day=2026-01-02, count=1, sum/avg=NULL",
          len(ex) == 1 and ex[0]["day"] == "2026-01-02" and ex[0]["count"] == 1
          and ex[0]["amount_sum"] is None and ex[0]["amount_avg"] is None,
          json.dumps(ex))

    # 9) 回退演示：去掉 v_detail/v_daily 后，b_only 无可用候选 → 明确回退 fact
    model2 = rollup.Model(model.fact, model.snapshot, model.dimensions,
                          tuple(v for v in model.views
                                if v.name in ("v_a", "v_avg", "v_stale")))
    q_b = {"id": "demo_fallback", "group_by": ["day"],
           "where": {"product": ["B"]}, "measures": ["count", "amount_avg"]}
    p2 = rollup.plan(model2, q_b, conn)
    e2, t2 = rollup.run(conn, model2, q_b, p2)
    d2, _ = rollup.direct_fact(conn, model2, q_b)
    ok2, _ = rollup.reconcile(e2, d2, q_b)
    check("回退演示: 无可用候选时明确回退 fact 且结果与直算一致",
          p2.fallback and p2.chosen == "fact" and p2.fallback_reason
          and t2 == {"fact"} and ok2, p2.fallback_reason or "")

    # 10) 全部查询与独立 fact 直算逐组对账一致
    check("全部查询与独立 fact 直算对账一致",
          all(r["reconcile"]["ok"] for r in results),
          "; ".join(r["query_id"] for r in results if not r["reconcile"]["ok"])
          or "全部一致")
    return checks


def main():
    db = ROOT / "work.db"
    if db.exists():
        db.unlink()
    subprocess.run([sys.executable, str(ROOT / "prepare.py"), str(db)], check=True)
    model = rollup.load_model(ROOT / "fixtures/model.json")
    queries = json.loads((ROOT / "fixtures/queries.json").read_text(encoding="utf-8"))
    queries.append(EXTRA_QUERY)
    conn = sqlite3.connect(db)
    try:
        results = run_all(conn, model, queries)
        checks = verify(model, conn, results)
    finally:
        conn.close()
    report = {
        "note": "amount_avg 输出 num/den 精确分数；decimal6 为声明 6 位精度的小数",
        "queries": results,
        "verifications": checks,
        "all_ok": all(c["ok"] for c in checks)
        and all(r["reconcile"]["ok"] for r in results),
    }
    (ROOT / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for r in results:
        p = r["plan"]
        print(f"[{r['query_id']}] 用表={p['chosen']} 残余过滤={p['residual_where']} "
              f"对账={'OK' if r['reconcile']['ok'] else r['reconcile']['diffs']}")
        for c in p["candidates"]:
            mark = "可用" if c["usable"] else "拒绝"
            print(f"    {c['name']:<9} {mark}  {c['reason']}")
    for c in checks:
        print(f"  {'PASS' if c['ok'] else 'FAIL'}  {c['check']}"
              + (f"  — {c['detail']}" if c['detail'] and not c['ok'] else ""))
    print(f"\n总体: {'全部通过' if report['all_ok'] else '存在失败'}，"
          f"详见 report.json")
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
