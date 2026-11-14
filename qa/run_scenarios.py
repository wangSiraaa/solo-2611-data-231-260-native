#!/usr/bin/env python3
"""Independent acceptance harness for extract.py.

Starts fixtures/mock_search.py, drives extract.py's open/collect/inspect
entry points for the four scenarios, and validates the results against the
server's own frozen truth log.  This harness is QA-only: the collector itself
never reads scenarios.json or the truth log.
"""
from __future__ import annotations

import collections
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACT = ROOT / "extract.py"
MOCK = ROOT / "fixtures" / "mock_search.py"
OUT = ROOT / "out"
FIELDS = ("record_id", "t", "bucket", "payload")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_server(tmp: Path):
    port = free_port()
    ready = tmp / "ready.json"
    req_log = tmp / "requests.jsonl"
    truth_log = tmp / "truth.jsonl"
    proc = subprocess.Popen(
        [sys.executable, str(MOCK), "--port", str(port),
         "--ready-file", str(ready), "--request-log", str(req_log),
         "--truth-log", str(truth_log)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    for _ in range(200):
        if ready.exists():
            break
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read().decode())
        time.sleep(0.05)
    else:
        proc.kill()
        raise RuntimeError("mock server did not become ready")
    return proc, json.loads(ready.read_text())["base_url"], req_log, truth_log


def run_cli(*args: str):
    proc = subprocess.run([sys.executable, str(EXTRACT), *args],
                          capture_output=True, text=True, timeout=120)
    parsed = {}
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError:
            parsed = {"_raw": proc.stdout.strip()}
    return proc.returncode, parsed, proc.stderr.strip()


def latest_truth(truth_log: Path):
    lines = [l for l in truth_log.read_text(encoding="utf-8").splitlines() if l]
    return json.loads(lines[-1])


def tree_of(out: Path) -> dict:
    return json.loads((out / "tree.json").read_text(encoding="utf-8"))


def coverage_of(out: Path) -> dict:
    return json.loads((out / "coverage.json").read_text(encoding="utf-8"))


def jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


# --- independent checks ----------------------------------------------------

def check_partition(tree: dict, fails: list[str], label: str) -> None:
    nodes = {n["node_id"]: n for n in tree["nodes"]}
    domain = tree["domain"]
    cells: set[tuple[int, int]] = set()

    def walk(nid: int) -> None:
        node = nodes[nid]
        if node["status"] in ("accepted", "saturated"):
            for t in range(node["t_range"][0], node["t_range"][1]):
                for b in range(node["bucket_range"][0], node["bucket_range"][1]):
                    if (t, b) in cells:
                        fails.append(f"{label}: cell {(t, b)} covered twice")
                    cells.add((t, b))
            return
        if node["status"] != "internal":
            fails.append(f"{label}: open node {nid} ({node['status']}) in tree")
            return
        for cid in node["children"]:
            walk(cid)

    walk(tree["root_node"])
    expected = {(t, b)
                for t in range(domain["t_range"][0], domain["t_range"][1])
                for b in range(domain["bucket_range"][0], domain["bucket_range"][1])}
    if cells != expected:
        fails.append(f"{label}: leaves miss cells {sorted(expected - cells)[:5]}")

    for node in tree["nodes"]:
        if node["status"] != "internal":
            continue
        child_sum = sum(nodes[c]["count"] for c in node["children"])
        if node["count"] != child_sum:
            fails.append(f"{label}: node {node['node_id']} count "
                         f"{node['count']} != children {child_sum}")


def check_rows(rows: list[dict], truth: list[dict], fails: list[str], label: str) -> None:
    got = collections.Counter(r["record_id"] for r in rows)
    want = collections.Counter(r["record_id"] for r in truth)
    if got != want:
        missing = list((want - got).elements())[:5]
        extra = list((got - want).elements())[:5]
        fails.append(f"{label}: id multiset mismatch missing={missing} extra={extra}")
    ref = {r["record_id"]: r for r in truth}
    for row in rows:
        if set(row) != set(FIELDS):
            fails.append(f"{label}: row {row.get('record_id')} fields {sorted(row)}")
            continue
        for f in FIELDS:
            if type(row[f]) is not type(ref.get(row["record_id"], {}).get(f)):
                fails.append(f"{label}: row {row['record_id']} field {f} type differs")
            elif row["record_id"] in ref and row[f] != ref[row["record_id"]][f]:
                fails.append(f"{label}: row {row['record_id']} field {f} differs")
    if [r["record_id"] for r in rows] != sorted(r["record_id"] for r in rows):
        fails.append(f"{label}: records not sorted by record_id")


def buffered_nonempty(tree: dict) -> list[dict]:
    return [n for n in tree["nodes"] if n["status"] == "saturated"]


# --- scenarios -------------------------------------------------------------

def open_and_collect(base: str, scenario: str, out: Path, truth_log: Path, fails):
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    rc, rec, err = run_cli("open", "--base-url", base, "--scenario", scenario,
                           "--out-dir", str(out))
    if rc != 0:
        fails.append(f"{scenario}: open rc={rc} err={err}")
        return None, None, None
    truth_rec = latest_truth(truth_log)
    if truth_rec["snapshot_id"] != rec.get("snapshot_id"):
        fails.append(f"{scenario}: opened snapshot does not match truth log")
    rc, rep, err = run_cli("collect", "--base-url", base, "--out-dir", str(out))
    return rc, rec, (rep, err)


def inspect_run(out: Path, fails: list[str], label: str):
    rc, audit, err = run_cli("inspect", "--out-dir", str(out))
    if not audit.get("ok"):
        fails.append(f"{label}: inspect not ok: {audit} {err}")
    return rc, audit


def scenario_normal(base, out, truth_log, fails):
    rc, rec, got = open_and_collect(base, "normal", out, truth_log, fails)
    if got is None:
        return
    rep, err = got
    truth = latest_truth(truth_log)["rows"]
    if rc != 0 or not rep.get("complete"):
        fails.append(f"normal: collect rc={rc} complete={rep.get('complete')} {err}")
    rc2, audit = inspect_run(out, fails, "normal")
    if rc2 != 0:
        fails.append(f"normal: inspect exit {rc2}")
    tree = tree_of(out)
    check_partition(tree, fails, "normal")
    check_rows(jsonl(out / "records.jsonl"), truth, fails, "normal")
    if rep.get("root_count") != len(truth):
        fails.append("normal: root_count != truth size")


def scenario_hotspot(base, out, truth_log, fails):
    rc, rec, got = open_and_collect(base, "hotspot", out, truth_log, fails)
    if got is None:
        return
    rep, err = got
    truth = latest_truth(truth_log)["rows"]
    if rc != 0 or not rep.get("complete"):
        fails.append(f"hotspot: collect rc={rc} complete={rep.get('complete')} {err}")
    inspect_run(out, fails, "hotspot")
    tree = tree_of(out)
    check_partition(tree, fails, "hotspot")
    check_rows(jsonl(out / "records.jsonl"), truth, fails, "hotspot")
    nodes = {n["node_id"]: n for n in tree["nodes"]}
    # The 12-record hotspot at t=7 must be resolved by splitting bucket after
    # t was narrowed to a single integer.
    bucket_splits = [
        n for n in nodes.values()
        if n["status"] == "internal"
        and n["t_range"] == [7, 8]
        and n["bucket_range"][1] - n["bucket_range"][0] > 1
        and all(nodes[c]["t_range"] == [7, 8] for c in n["children"])
        and all(nodes[c]["bucket_range"] != n["bucket_range"] for c in n["children"])
    ]
    if not bucket_splits:
        fails.append("hotspot: no second-dimension (bucket) split inside t=7")
    # No node may claim complete rows while count > cap except when saturated
    # and visibly marked; a hotspot run must have no saturated leaves.
    if buffered_nonempty(tree):
        fails.append("hotspot: unexpected saturated leaves")
    if rep.get("missing_count") != 0:
        fails.append("hotspot: missing_count should be 0")
    # Ensure the hotspot records really came from bucket-level leaves.
    hot_ids = {r["record_id"] for r in truth if r["t"] == 7}
    got_ids = {r["record_id"] for r in jsonl(out / "records.jsonl")}
    if not hot_ids <= got_ids:
        fails.append("hotspot: not all hotspot records retrieved")


def scenario_saturated(base, out, truth_log, fails):
    rc, rec, got = open_and_collect(base, "saturated", out, truth_log, fails)
    if got is None:
        return
    rep, err = got
    truth = latest_truth(truth_log)["rows"]
    if rc != 2:
        fails.append(f"saturated: expected exit 2, got {rc}")
    if rep.get("complete") is not False:
        fails.append("saturated: complete must be false")
    if (out / "records.jsonl").exists():
        fails.append("saturated: canonical records.jsonl must not exist")
    if not (out / "records.jsonl.partial").exists():
        fails.append("saturated: partial records must be retained")
    cov = coverage_of(out)
    if cov.get("complete") is not False:
        fails.append("saturated: coverage.complete must be false")
    cell_counts = collections.Counter((r["t"], r["bucket"]) for r in truth)
    expected_missing = sum(max(0, n - 4) for n in cell_counts.values())
    if rep.get("missing_count") != expected_missing:
        fails.append(f"saturated: missing_count {rep.get('missing_count')} "
                     f"!= {expected_missing}")
    sat = coverage_of(out).get("saturated_ranges", [])
    if len(sat) != 1:
        fails.append(f"saturated: expected one saturated cell, got {sat}")
    else:
        s = sat[0]
        if s["t_range"] != [2, 3] or s["bucket_range"] != [2, 3]:
            fails.append(f"saturated: wrong saturated cell {s}")
        if s["unretrieved"] != expected_missing:
            fails.append(f"saturated: unretrieved {s['unretrieved']} != {expected_missing}")
        if s["count"] != cell_counts[(2, 2)]:
            fails.append("saturated: saturated count != truth cell count")
    rc2, audit = inspect_run(out, fails, "saturated")
    if rc2 != 0:
        fails.append(f"saturated: inspect exit {rc2} (expected 0 for consistent diagnostics)")
    if audit.get("declared_complete") is not False:
        fails.append("saturated: inspect must read declared_complete=false")
    tree = tree_of(out)
    check_partition(tree, fails, "saturated")
    partial = jsonl(out / "records.jsonl.partial")
    truth_ids = {r["record_id"] for r in truth}
    if not {r["record_id"] for r in partial} <= truth_ids:
        fails.append("saturated: partial contains ids not in truth")
    if len(partial) != rep.get("leaf_rows"):
        fails.append("saturated: partial rows != coverage.leaf_rows")
    if len(partial) + cov.get("saturated_visible_rows", 0) + \
            rep.get("missing_count", 0) != len(truth):
        fails.append("saturated: accepted + saturated-visible + missing != truth total")
    # Each saturated leaf must have exactly cap visible rows and no child.
    for n in tree["nodes"]:
        if n["status"] == "saturated":
            if len(n["rows"]) != 4 or n["children"]:
                fails.append(f"saturated: bad saturated leaf {n['node_id']}")


def scenario_expired(base, out, truth_log, fails):
    rc, rec, got = open_and_collect(base, "expired", out, truth_log, fails)
    if got is None:
        return
    rep, err = got
    if rc != 2:
        fails.append(f"expired: expected exit 2, got {rc}")
    if rep.get("complete") is not False:
        fails.append("expired: complete must be false")
    kinds = {e.get("kind") for e in coverage_of(out).get("errors", [])}
    if "snapshot_expired" not in kinds:
        fails.append(f"expired: snapshot_expired not recorded ({kinds})")
    if (out / "records.jsonl").exists():
        fails.append("expired: canonical records.jsonl must not exist")
    if not (out / "records.jsonl.partial").exists():
        fails.append("expired: partial records must be retained")
    if coverage_of(out).get("complete") is not False:
        fails.append("expired: coverage.complete must be false")
    rc2, audit = inspect_run(out, fails, "expired")
    if rc2 != 0:
        fails.append(f"expired: inspect exit {rc2} (expected 0 for consistent diagnostics)")
    if audit.get("declared_complete") is not False or not audit.get("ok"):
        fails.append(f"expired: inspect must read a consistent incomplete run: {audit}")
    if rep.get("query_count") != 4:
        fails.append(f"expired: expected 4 search attempts, got {rep.get('query_count')}")
    queries = jsonl(out / "queries.jsonl")
    if len(queries) != 4:
        fails.append(f"expired: queries.jsonl has {len(queries)} lines")
    for q in queries:
        if q["request"]["snapshot_id"] != rec["snapshot_id"]:
            fails.append("expired: a query escaped the opened snapshot")
        if q["http_status"] not in (200, 410):
            fails.append(f"expired: unexpected status {q['http_status']}")
    # The first three calls succeeded; nothing may pretend they cover the domain.
    accepted = [n for n in tree_of(out)["nodes"] if n["status"] == "accepted"]
    if len(accepted) >= 1 and rep.get("complete"):
        fails.append("expired: partial accepted leaves reported complete")


def main() -> int:
    OUT.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="capped-qa-"))
    proc = None
    failures: list[str] = []
    try:
        proc, base, req_log, truth_log = start_server(tmp)
        scenarios = {
            "normal": scenario_normal,
            "hotspot": scenario_hotspot,
            "saturated": scenario_saturated,
            "expired": scenario_expired,
        }
        for name, fn in scenarios.items():
            before = len(failures)
            fn(base, OUT / name, truth_log, failures)
            print(f"{name:10s} {'PASS' if len(failures) == before else 'FAIL'}")
        # Collector must only ever have touched /open and /search.
        for line in jsonl(req_log):
            if line["path"] not in ("/open", "/search"):
                failures.append(f"collector touched forbidden endpoint {line['path']}")
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL SCENARIOS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
