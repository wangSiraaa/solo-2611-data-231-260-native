#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验收脚本：独立运行 writer 与采集器，用 truth 文件核对采集结果。

truth 是 writer 独立生成的验收数据，采集器从不读取；本脚本是唯一同时
接触两者的组件。覆盖三个场景：rename / copytruncate / partial。
"""
from __future__ import annotations

import base64
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COLLECTOR = ROOT / "collector.py"
WRITER = ROOT / "fixtures" / "writer.py"
CONFIG = {
    "rename": ROOT / "fixtures" / "rename-config.json",
    "copytruncate": ROOT / "fixtures" / "copytruncate-config.json",
    "partial": ROOT / "fixtures" / "partial-config.json",
}

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_truth(path: Path):
    """truth 按 (dev, inode, generation) 分组拼接，供与采集来源范围核对。"""
    writes = load_jsonl(path)
    events: dict[str, list[dict]] = {}
    by_key: dict[tuple, list[dict]] = {}
    for w in writes:
        events.setdefault(w["event"], []).append(w)
        by_key.setdefault((w["dev"], w["inode"], w["generation"]), []).append(w)
    streams = {}
    for key, ws in by_key.items():
        ws.sort(key=lambda x: x["start"])
        ok = ws[0]["start"] == 0 and all(ws[i]["end"] == ws[i + 1]["start"] for i in range(len(ws) - 1))
        check(ok, f"truth 流 {key} 自身连续（验收数据自检）")
        streams[key] = b"".join(base64.b64decode(w["bytes_base64"]) for w in ws)
    return streams, events


def run_scenario(name: str) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"verify-{name}-"))
    log_dir = tmp / "logs"
    log_dir.mkdir()
    state_dir = tmp / "state"
    truth_file = tmp / "truth.jsonl"
    stop_file = tmp / "stop"
    export_prefix = tmp / "export" / "export"
    export_prefix.parent.mkdir(parents=True)

    coll = subprocess.Popen(
        [sys.executable, str(COLLECTOR), "run", "--config", str(CONFIG[name]),
         "--log-dir", str(log_dir), "--stop-file", str(stop_file),
         "--state-dir", str(state_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.2)  # 采集器先进入轮询；writer 另有 start-delay
    writer = subprocess.Popen(
        [sys.executable, str(WRITER), "--scenario", name, "--log-dir", str(log_dir),
         "--truth-file", str(truth_file), "--stop-file", str(stop_file)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    writer_log: list[tuple[float, str]] = []

    def _reader():
        for line in writer.stdout:
            writer_log.append((time.monotonic(), line.strip()))

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()

    observations: list[tuple[float, dict]] = []
    deadline = time.monotonic() + 90
    while coll.poll() is None:
        if time.monotonic() > deadline:
            coll.kill()
            check(False, f"{name}: 采集器在 stop 后未及时排空退出")
            break
        try:
            observations.append((time.monotonic(), json.loads(
                (state_dir / "state.json").read_text(encoding="utf-8"))))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(0.05)

    coll_out, coll_err = coll.communicate(timeout=15)
    writer.wait(timeout=30)
    reader.join(timeout=5)
    check(coll.returncode == 0, f"{name}: 采集器退出码为 0", coll_err)
    check(writer.returncode == 0, f"{name}: writer 退出码为 0",
          writer.stderr.read() if writer.stderr else "")

    st = subprocess.run([sys.executable, str(COLLECTOR), "status",
                         "--state-dir", str(state_dir)], capture_output=True, text=True)
    check(st.returncode == 0, f"{name}: status 命令正常", st.stderr.strip())
    status = json.loads(st.stdout) if st.returncode == 0 else {}

    ex = subprocess.run([sys.executable, str(COLLECTOR), "export",
                         "--state-dir", str(state_dir), "--out-prefix", str(export_prefix)],
                        capture_output=True, text=True)
    check(ex.returncode == 0, f"{name}: export 命令正常", ex.stderr.strip())

    base = str(export_prefix)
    return {
        "records": load_jsonl(Path(base + ".records.jsonl")),
        "canonical": {e["seq"]: e["canonical_json"] for e in load_jsonl(Path(base + ".canonical.jsonl"))},
        "invalid": load_jsonl(Path(base + ".invalid.jsonl")),
        "report": json.loads(Path(base + ".report.json").read_text(encoding="utf-8")),
        "status": status,
        "truth_streams": load_truth(truth_file)[0],
        "truth_events": load_truth(truth_file)[1],
        "writer_log": writer_log,
        "observations": observations,
        "coll_out": coll_out,
    }


def check_common(name: str, r: dict) -> None:
    records, invalid, report = r["records"], r["invalid"], r["report"]

    wellformed = True
    for rec in records:
        raw = base64.b64decode(rec["raw_base64"])
        if not raw.endswith(b"\n") or rec["end_offset"] - rec["start_offset"] != len(raw):
            wellformed = False
            break
        obj = json.loads(raw.decode("utf-8"))
        canon = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if r["canonical"].get(rec["seq"]) != canon:
            wellformed = False
            break
    check(wellformed, f"{name}: 记录均为 LF 结尾的合法 UTF-8/JSON，范围含 LF，规范 JSON 与原始字节分别保存且一致")

    coll: dict[tuple, list[dict]] = {}
    for e in records + invalid:
        coll.setdefault((e["dev"], e["inode"], e["generation"]), []).append(e)
    check(set(coll) == set(r["truth_streams"]), f"{name}: 采集流集合与 truth 一致",
          f"collector={sorted(coll)} truth={sorted(r['truth_streams'])}")
    for key, entries in sorted(coll.items()):
        entries.sort(key=lambda e: e["start_offset"])
        contiguous = entries[0]["start_offset"] == 0 and all(
            entries[i]["end_offset"] == entries[i + 1]["start_offset"]
            for i in range(len(entries) - 1))
        check(contiguous, f"{name}: 流 {key} 字节范围自 0 连续无重叠（每字节恰好采集一次）")
        data = b"".join(base64.b64decode(e["raw_base64"]) for e in entries)
        check(data == r["truth_streams"].get(key),
              f"{name}: 流 {key} 拼接字节与 writer truth 完全一致")
    for s in report["streams"]:
        key = (s["dev"], s["inode"], s["generation"])
        if key in r["truth_streams"]:
            check(s["confirmed_offset"] == len(r["truth_streams"][key]),
                  f"{name}: 流 {key} 确认偏移 == truth 长度")
    check(report["finished"] is True and report["collected_records"] == len(records),
          f"{name}: 报告已完成且收集行数自洽")


def check_rename(r: dict) -> None:
    report = r["report"]
    check(report["rotation_mode"] == "rename", "rename: 报告模式正确")
    streams = report["streams"]
    check(len(streams) == 2 and len({s["inode"] for s in streams}) == 2,
          "rename: 恰好两个不同 inode 的流（旧句柄 + 新 active）")
    old = next((s for s in streams if "active.log.1" in s["paths"]), None)
    check(old is not None and set(old["paths"]) == {"active.log", "active.log.1"},
          "rename: 旧 inode 改名后只登记别名，未作为新流重复导入")
    if old is not None:
        new = next(s for s in streams if s["inode"] != old["inode"])
        check(old["generation"] == 0 and new["generation"] == 1,
              "rename: 旧文件代为 0、新 active 代为 1，各自独立 offset")
    check(not report["discontinuities"] and not report["unterminated_tails"],
          "rename: 无 discontinuity、无未终结残尾")

    part1 = base64.b64decode(r["truth_events"]["old-partial-before-rename"][0]["bytes_base64"])
    part2 = base64.b64decode(r["truth_events"]["old-finish-after-rename"][0]["bytes_base64"])
    matched = [rec for rec in r["records"]
               if base64.b64decode(rec["raw_base64"]) == part1 + part2
               and rec["start_offset"] == r["truth_events"]["old-partial-before-rename"][0]["start"]
               and rec["end_offset"] == r["truth_events"]["old-finish-after-rename"][0]["end"]]
    check(len(matched) == 1, "rename: 跨改名的中文行从同一旧文件补齐为恰好一条记录，范围跨改名点")
    if matched:
        check(json.loads(r["canonical"][matched[0]["seq"]])["message"] == "old 中文 tail",
              "rename: 中文行内容正确")
    check(len(r["records"]) == 5 and not r["invalid"],
          "rename: 改名后旧文件追加与新 active 交错共 5 行，全部恰好收集一次")


def check_copytruncate(r: dict) -> None:
    report = r["report"]
    check(report["rotation_mode"] == "copytruncate", "copytruncate: 报告模式正确")
    streams = report["streams"]
    check(len(streams) == 2 and len({s["inode"] for s in streams}) == 1,
          "copytruncate: 同一 inode 分为两代")
    check(all(s["paths"] == ["active.log"] for s in streams)
          and all(set(rec["paths"]) == {"active.log"} for rec in r["records"]),
          "copytruncate: 只采集 active.log，复制归档未当作新输入")

    discs = report["discontinuities"]
    check(len(discs) == 1, "copytruncate: 报告恰好一次 discontinuity")
    if discs:
        d = discs[0]
        gen0_key = next(k for k in r["truth_streams"] if k[2] == 0)
        check(d["old_confirmed_offset"] == len(r["truth_streams"][gen0_key]),
              "copytruncate: discontinuity 报告旧确认偏移")
        check(d["reason"] in ("size_shrink", "prefix_checksum_changed", "inode_changed"),
              "copytruncate: discontinuity 触发原因合法")
        check(d.get("continuity") == "unprovable",
              "copytruncate: 边界标记为无法证明连续")
    check("无法证明" in report["continuity_statement"],
          "copytruncate: 连续性说明不声称无丢失")
    check("业务 id" in report["gap_policy"],
          "copytruncate: 声明不按业务 id 猜补缺口")
    check(report["collected_records"] == 4 and report["isolated_lines"] == 0
          and not report["discarded_tails"] and not report["unterminated_tails"],
          "copytruncate: 本样例 4 行实际收集齐全，但报告仍区分收集行与无法证明的边界")


def check_partial(r: dict) -> None:
    ts = {line: t for t, line in r["writer_log"]}
    check("partial-final-LF" in ts and "partial-first" in ts,
          "partial: writer 阶段日志完整")
    if "partial-final-LF" in ts and "partial-first" in ts:
        t_lf = ts["partial-final-LF"]
        pre = [(t, s) for t, s in r["observations"] if t < t_lf]
        check(len(pre) > 0, "partial: 最终 LF 前已有状态观测（采集器在运行）")
        check(any(s["records"] == 0
                  and sum(x["pending_tail_bytes"] for x in s["streams"]) > 0
                  for _, s in pre),
              "partial: 半条中文记录只暂存未发布")
        check(all(s["records"] == 0 for _, s in pre),
              "partial: 最终 LF 到达前记录表为空（未提前发布）")

    check(len(r["invalid"]) == 1, "partial: 坏 JSON 按完整行隔离恰好一条")
    if r["invalid"]:
        bad = r["invalid"][0]
        truth_bad = base64.b64decode(r["truth_events"]["invalid-json-complete-line"][0]["bytes_base64"])
        check(base64.b64decode(bad["raw_base64"]) == truth_bad and bad["reason"] == "bad_json",
              "partial: 坏行原始字节与隔离原因正确")
        recs = sorted(r["records"], key=lambda e: e["start_offset"])
        check(len(recs) == 2, "partial: 两条合法记录")
        if len(recs) == 2:
            check(recs[0]["end_offset"] == bad["start_offset"]
                  and bad["end_offset"] == recs[1]["start_offset"],
                  "partial: 坏行隔离后后续行偏移连续无错位")
            msgs = [json.loads(r["canonical"][rec["seq"]])["message"] for rec in recs]
            check(msgs == ["prefix 中文 suffix", "valid after bad"],
                  "partial: 中文行完整、后续行内容正确")
    check(not r["report"]["unterminated_tails"], "partial: 无未终结残尾")


def main() -> int:
    for name in ("rename", "copytruncate", "partial"):
        print(f"=== 场景 {name} ===")
        r = run_scenario(name)
        check_common(name, r)
        {"rename": check_rename,
         "copytruncate": check_copytruncate,
         "partial": check_partial}[name](r)
        print()
    if failures:
        print(f"{len(failures)} 项失败:")
        for f in failures:
            print(" -", f)
        return 1
    print("全部验收通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
