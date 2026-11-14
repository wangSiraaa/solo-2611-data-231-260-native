#!/usr/bin/env python3
"""端到端自检：给定 fixtures 主例、违例拒绝、顺序无关性、两个小例、输入规格拒绝。"""

import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOL = ROOT / "decompose.py"
FIX = ROOT / "fixtures"

failures = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name)


def run(schema, *extra):
    return subprocess.run(
        [sys.executable, str(TOOL), str(schema), *[str(e) for e in extra]],
        capture_output=True, text=True, cwd=ROOT)


def dump_db(db):
    conn = sqlite3.connect(db)
    tables = [t[0] for t in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    data = {t: sorted(conn.execute(f'SELECT * FROM "{t}"')) for t in tables}
    conn.close()
    return data


tmp = Path(tempfile.mkdtemp(prefix="decompose_selftest_"))
print(f"临时目录: {tmp}\n")

# 1. 主例（fixtures/schema.json + fixtures/records.csv） ----------------------
out1 = tmp / "main"
r = run(FIX / "schema.json", "--out", out1)
check("主例运行成功", r.returncode == 0, r.stderr)
plan = json.loads((out1 / "plan.json").read_text(encoding="utf-8"))

cover = {(tuple(f["lhs"]), f["rhs"]) for f in plan["minimal_cover"]["cover"]}
check("最小覆盖：给定冗余依赖已消除", cover == {
    (("employee",), "name"),
    (("employee",), "department"),
    (("department",), "office"),
    (("employee", "skill"), "rating"),
}, str(cover))

rels = {tuple(rel["attributes"]) for rel in plan["relations"]}
check("关系集合", rels == {
    ("employee", "name", "department"),
    ("department", "office"),
    ("employee", "skill", "rating"),
}, str(rels))
check("复合技能键即候选键，无需补键关系",
      plan["key_relation_added"] is None
      and plan["schema_candidate_keys"] == [["employee", "skill"]])
check("重建核对通过", plan["rebuild"]["verified"] is True
      and plan["rebuild"]["tuples_rebuilt"] == 4)
check("依赖保持", plan["dependency_preserving"] is True
      and all(d["derived"] for d in plan["dependency_derivations"]))

acts = [s["action"] for s in plan["minimal_cover"]["steps"]]
check("记录了左部消除", "remove_lhs_attribute" in acts)
lhs = next(s for s in plan["minimal_cover"]["steps"]
           if s["action"] == "remove_lhs_attribute")
check("左部消除带闭包依据（employee,skill->name 去掉 skill）",
      lhs["removed_attribute"] == "skill" and lhs["justification"]["steps"],
      json.dumps(lhs, ensure_ascii=False))
check("化简后与 employee->name 重复而丢弃",
      any(s["action"] == "drop_duplicate" for s in plan["minimal_cover"]["steps"]))
fd2 = next(d for d in plan["dependency_derivations"] if d["fd"].startswith("FD2"))
check("FD2(employee,skill->name) 由 rel_1 投影依赖推出",
      fd2["derived"] and any("rel_1" in s["fd"] for s in fd2["per_rhs"][0]["steps"]),
      json.dumps(fd2, ensure_ascii=False))

lineage = json.loads((out1 / "lineage.json").read_text(encoding="utf-8"))
e1 = next(x for x in lineage["relations"]["rel_1"] if x["tuple"]["employee"] == "e1")
check("来源映射记录原逻辑行号", e1["source_rows"] == [1, 2], str(e1))

db = dump_db(out1 / "decomposed.db")
check("库中只有分解后的关系（不存宽表）",
      set(db) == {"rel_1", "rel_2", "rel_3"}, str(set(db)))

# 2. fixtures/invalid.csv 拒绝发布 --------------------------------------------
out2 = tmp / "invalid_out"
r = run(FIX / "schema.json", "--csv", FIX / "invalid.csv", "--out", out2)
check("invalid.csv 退出码 1", r.returncode == 1, r.stderr)
check("定位姓名冲突（决定列值 + 两条矛盾源行）",
      all(s in r.stderr for s in ("name", "e1", "第 1 行", "第 2 行", "Ann", "Different")),
      r.stderr)
check("拒绝发布（无输出目录）", not out2.exists())

# 3. 顺序无关：依赖逆序 + CSV 行逆序 → 规范方案与库内容不变 --------------------
perm = tmp / "perm"
perm.mkdir()
spec = json.loads((FIX / "schema.json").read_text(encoding="utf-8"))
spec["dependencies"] = list(reversed(spec["dependencies"]))
(perm / "schema.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
lines = (FIX / "records.csv").read_text(encoding="utf-8").splitlines()
(perm / "records.csv").write_text(
    "\n".join([lines[0]] + list(reversed(lines[1:]))) + "\n", encoding="utf-8")
out3 = tmp / "perm_out"
r = run(perm / "schema.json", "--out", out3)
check("逆序输入运行成功", r.returncode == 0, r.stderr)
plan_p = json.loads((out3 / "plan.json").read_text(encoding="utf-8"))
strip = lambda p: {k: v for k, v in p.items() if k != "source"}
check("规范方案与顺序无关", strip(plan_p) == strip(plan))
check("数据库内容与顺序无关", dump_db(out1 / "decomposed.db") == dump_db(out3 / "decomposed.db"))

# 4. 小例：无非平凡依赖 --------------------------------------------------------
r = run(ROOT / "examples/no_nontrivial/schema.json", "--out", tmp / "nn")
plan_nn = json.loads((tmp / "nn/plan.json").read_text(encoding="utf-8"))
check("无非平凡依赖：最小覆盖为空", plan_nn["minimal_cover"]["cover"] == [])
check("无非平凡依赖：补入全属性键关系",
      plan_nn["key_relation_added"] == ["code", "label", "note"]
      and len(plan_nn["relations"]) == 1
      and plan_nn["relations"][0]["origin"] == "candidate_key")
check("无非平凡依赖：重建核对", plan_nn["rebuild"]["verified"] is True)

# 5. 小例：需要补键关系 --------------------------------------------------------
r = run(ROOT / "examples/needs_key/schema.json", "--out", tmp / "nk")
plan_nk = json.loads((tmp / "nk/plan.json").read_text(encoding="utf-8"))
rels_nk = sorted(tuple(x["attributes"]) for x in plan_nk["relations"])
check("补键关系例：关系集合", rels_nk == [("a", "b"), ("a", "c")], str(rels_nk))
check("补键关系例：补入按属性数、属性顺序最小的候选键",
      plan_nk["key_relation_added"] == ["a", "c"])
check("补键关系例：重建核对", plan_nk["rebuild"]["verified"] is True
      and plan_nk["rebuild"]["tuples_rebuilt"] == 3)

# 6. 输入规格拒绝：重复完整元组 / 空单元格 --------------------------------------
base = (FIX / "records.csv").read_text(encoding="utf-8").splitlines()
dup_csv = "\n".join(base + [base[1]]) + "\n"
empty_csv = "\n".join([base[0], base[1].replace("Ann", ""), *base[2:]]) + "\n"
for name, content, expect in [
    ("重复完整元组", dup_csv, "重复完整元组"),
    ("空单元格", empty_csv, "为空"),
]:
    d = tmp / f"bad_{name}"
    d.mkdir()
    s = json.loads((FIX / "schema.json").read_text(encoding="utf-8"))
    (d / "schema.json").write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    (d / "records.csv").write_text(content, encoding="utf-8")
    r = run(d / "schema.json", "--out", d / "out")
    check(f"拒绝{name}", r.returncode == 1 and expect in r.stderr and not (d / "out").exists(),
          r.stderr)

print()
if failures:
    print(f"{len(failures)} 项失败: {failures}")
    sys.exit(1)
print("全部自检通过。")
