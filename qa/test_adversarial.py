#!/usr/bin/env python3
"""Fault-injection tests: the collector must never report complete when the
endpoint lies, drifts, or errors.

Uses a small programmable server that speaks the documented /open + /search
contract but corrupts selected responses.  No fixture data files are read.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRACT = ROOT / "extract.py"

DOMAIN = [0, 8]
CAP = 4


class FaultServer:
    """Serve scripted /search responses.  ``faults`` maps a tuple
    (t_lo, t_hi, b_lo, b_hi) -> callable(body, attempt) -> body."""

    def __init__(self, rows, faults=None, fail_status=None, snapshot_id="snap-adv"):
        self.rows = rows
        self.faults = faults or {}
        self.fail_status = fail_status or {}
        self.snapshot_id = snapshot_id
        self.calls = 0
        self.seen_snapshot_ids = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body):
                raw = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                if self.path == "/open":
                    return self.reply(200, {
                        "snapshot_id": outer.snapshot_id, "scenario": "adv",
                        "cap": CAP, "t_range": DOMAIN,
                        "bucket_range": DOMAIN,
                    })
                if self.path != "/search":
                    return self.reply(404, {"error": "unknown_endpoint"})
                outer.calls += 1
                outer.seen_snapshot_ids.append(body.get("snapshot_id"))
                key = tuple(body["t_range"]) + tuple(body["bucket_range"])
                status = outer.fail_status.get(key)
                if status:
                    return self.reply(status, {"error": "injected"})
                selected = sorted(
                    (r for r in outer.rows
                     if body["t_range"][0] <= r["t"] < body["t_range"][1]
                     and body["bucket_range"][0] <= r["bucket"] < body["bucket_range"][1]),
                    key=lambda r: r["record_id"])
                resp = {
                    "snapshot_id": body["snapshot_id"],
                    "t_range": body["t_range"],
                    "bucket_range": body["bucket_range"],
                    "count": len(selected),
                    "cap": CAP,
                    "rows": selected[:CAP],
                }
                fault = outer.faults.get(key)
                if fault is not None:
                    fault(resp)
                return self.reply(200, resp)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


def drive(server: FaultServer) -> tuple[int, dict, dict, dict]:
    tmp = Path(tempfile.mkdtemp())
    out = tmp / "run"
    rc_open = subprocess.run(
        [sys.executable, str(EXTRACT), "open", "--base-url", server.base,
         "--scenario", "adv", "--out-dir", str(out)],
        capture_output=True, text=True, timeout=60)
    assert rc_open.returncode == 0, rc_open.stderr
    rc = subprocess.run(
        [sys.executable, str(EXTRACT), "collect", "--base-url", server.base,
         "--out-dir", str(out)],
        capture_output=True, text=True, timeout=60).returncode
    coverage = json.loads((out / "coverage.json").read_text())
    tree = json.loads((out / "tree.json").read_text())
    audit_rc = subprocess.run(
        [sys.executable, str(EXTRACT), "inspect", "--out-dir", str(out)],
        capture_output=True, text=True, timeout=60).returncode
    audit = json.loads(subprocess.run(
        [sys.executable, str(EXTRACT), "inspect", "--out-dir", str(out)],
        capture_output=True, text=True, timeout=60).stdout)
    return rc, coverage, {"tree": tree, "audit": audit, "audit_rc": audit_rc,
                          "published": (out / "records.jsonl").exists()}, audit


def rows_for(n: int, t: int = 0, bucket: int = 0) -> list[dict]:
    return [{"record_id": f"a{i:02d}", "t": t, "bucket": bucket, "payload": "x"}
            for i in range(n)]


def root_key():
    return tuple(DOMAIN) + tuple(DOMAIN)


FAILURES: list[str] = []


def expect_incomplete(label, server, want_kind=None):
    rc, cov, extra, _ = drive(server)
    if rc != 2:
        FAILURES.append(f"{label}: expected exit 2, got {rc}")
    if cov.get("complete") is not False:
        FAILURES.append(f"{label}: complete must be false")
    if extra["published"]:
        FAILURES.append(f"{label}: must not publish records.jsonl")
    if extra["audit_rc"] != 0 or not extra["audit"].get("ok"):
        FAILURES.append(f"{label}: inspect flagged inconsistent artifacts "
                        f"{extra['audit'].get('problems')}")
    if cov.get("fatal") is None:
        FAILURES.append(f"{label}: fatal cause not recorded")
    elif want_kind and want_kind not in (cov["fatal"].get("kind") or ""):
        FAILURES.append(f"{label}: fatal kind {cov['fatal'].get('kind')} != {want_kind}")
    if server.seen_snapshot_ids and any(
            sid != server.snapshot_id for sid in server.seen_snapshot_ids):
        FAILURES.append(f"{label}: query used a different snapshot_id")


def test_echo_mismatch():
    def fault(resp):
        resp["t_range"] = [0, 1]
    with FaultServer(rows_for(8), {root_key(): fault}) as s:
        expect_incomplete("echo_mismatch", s, "echo_mismatch")


def test_wrong_snapshot_echo():
    def fault(resp):
        resp["snapshot_id"] = "some-other"
    with FaultServer(rows_for(8), {root_key(): fault}) as s:
        expect_incomplete("wrong_snapshot", s, "echo_mismatch")


def test_count_mismatch():
    # Root says 9 but the two children only account for 8 records.
    rows = rows_for(8)
    def fault(resp):
        resp["count"] = 9
    with FaultServer(rows, {root_key(): fault}) as s:
        expect_incomplete("count_mismatch", s, "count_mismatch")


def test_row_outside_range():
    def fault(resp):
        resp["rows"] = [{"record_id": "z", "t": 99, "bucket": 0, "payload": "x"}]
        resp["count"] = 1
    with FaultServer(rows_for(8), {root_key(): fault}) as s:
        expect_incomplete("row_out_of_range", s, "row_out_of_range")


def test_duplicate_record_in_leaf():
    # A leaf may legitimately hold several ids at one coordinate, but the same
    # id twice is corruption and must abort the run.
    def fault(resp):
        resp["rows"] = [dict(row) for row in (resp["rows"][:1] * 2)]
        resp["count"] = 2
    with FaultServer(rows_for(2, t=0, bucket=0), {root_key(): fault}) as s:
        expect_incomplete("duplicate_record_id", s, "duplicate_record_id")


def test_bad_field_type():
    def fault(resp):
        resp["rows"] = [{"record_id": "z", "t": "0", "bucket": 0, "payload": "x"}]
        resp["count"] = 1
    with FaultServer(rows_for(8), {root_key(): fault}) as s:
        expect_incomplete("bad_field_type", s, "bad_field_type")


def test_missing_row_in_accepted_leaf():
    rows = rows_for(3)
    def fault(resp):
        resp["rows"] = resp["rows"][:-1]
    with FaultServer(rows, {root_key(): fault}) as s:
        expect_incomplete("missing_row", s, "bad_row_count")


def test_unknown_snapshot():
    with FaultServer(rows_for(2), fail_status={root_key(): 404}) as s:
        expect_incomplete("unknown_snapshot", s, "unknown_snapshot")


def test_expired_midway():
    # First split succeeds, a later subquery expires.
    with FaultServer(rows_for(8), fail_status={(0, 4, 0, 8): 410}) as s:
        expect_incomplete("expired_midway", s, "snapshot_expired")


def test_empty_domain_is_complete():
    with FaultServer([]) as s:
        rc, cov, extra, _ = drive(s)
        if rc != 0 or cov.get("complete") is not True:
            FAILURES.append("empty: an empty snapshot must be complete")
        if extra["audit_rc"] != 0 or not extra["audit"].get("ok"):
            FAILURES.append("empty: inspect should pass")
        if not extra["published"]:
            FAILURES.append("empty: complete run must publish (empty) records.jsonl")


def test_saturated_not_published():
    with FaultServer(rows_for(6)) as s:
        rc, cov, extra, audit = drive(s)
        if rc != 2 or cov.get("complete") is not False:
            FAILURES.append("saturated: must be incomplete")
        if cov.get("missing_count") != 2 or cov.get("saturated_leaf_count") != 1:
            FAILURES.append(f"saturated: wrong accounting {cov}")
        if extra["published"]:
            FAILURES.append("saturated: must not publish records.jsonl")
        if extra["audit_rc"] != 0 or not audit.get("ok"):
            FAILURES.append(f"saturated: diagnostic audit should pass {audit}")


def main() -> int:
    tests = [
        test_echo_mismatch, test_wrong_snapshot_echo, test_count_mismatch,
        test_row_outside_range, test_duplicate_record_in_leaf, test_bad_field_type,
        test_missing_row_in_accepted_leaf, test_unknown_snapshot,
        test_expired_midway, test_empty_domain_is_complete,
        test_saturated_not_published,
    ]
    for test in tests:
        before = len(FAILURES)
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - report and continue
            FAILURES.append(f"{test.__name__}: raised {exc!r}")
        print(f"{test.__name__:38s} {'PASS' if len(FAILURES) == before else 'FAIL'}")
    if FAILURES:
        print("\nFAILURES:")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("\nALL ADVERSARIAL TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
