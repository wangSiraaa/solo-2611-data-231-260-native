#!/usr/bin/env python3
"""Complete extraction from a capped, frozen search endpoint.

The endpoint contract (see fixtures/README.md):

  POST /open   {"scenario": "..."} -> snapshot identity + cap + whole domain
  POST /search {"snapshot_id": ..., "t_range": [lo, hi), "bucket_range": [lo, hi)}
               -> exact count, at most cap rows, snapshot/range echo

There is no cursor and re-sending the same query never yields later rows.  The
only sound way to get a region larger than ``cap`` is to split that region into
two disjoint integer ranges whose union is exactly the parent, query both, and
recurse.  A response is accepted as a leaf only when ``count <= cap``; then the
returned rows are the complete contents of that leaf (subject to the checks
below).  A one-cell region (one t value and one bucket value) with
``count > cap`` is reported as ``saturated`` with an explicit number of records
that cannot be retrieved through this endpoint.  Such a run is never reported
as complete.

Split strategy
--------------
Time-major midpoint bisection.  While the t interval contains more than one
integer we split t at its integer midpoint.  Only after t has been reduced to a
single integer do we split bucket at its midpoint.  A one-cell region is
therefore the only terminal-with-too-many-rows case.  This deliberately makes
the "same t, many buckets" hotspot require second-dimension refinement, rather
than trying to optimise query count (which is explicitly out of scope).

Every node, its request, the response echoed by the server, and the split
children are persisted in tree.json; queries.jsonl holds the raw request and
response bodies so every accepted leaf can be re-audited.

Entry points
------------
  extract.py open    ...   open one snapshot and record its identity/domain
  extract.py collect ...   partition the whole domain and write artifacts
  extract.py inspect ...   structurally re-audit the saved tree/coverage
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

EXIT_OK = 0
EXIT_INCOMPLETE = 2
EXIT_USAGE = 64

FIELD_DEFS = (("record_id", str), ("t", int), ("bucket", int), ("payload", str))
# Every frozen row the service can return has exactly these fields/types.  A
# response that deviates is rejected instead of being silently accepted.
EXPECTED_FIELDS = {name for name, _ in FIELD_DEFS}


class ExtractError(Exception):
    """A failure that makes the current snapshot's result unpublishable."""

    def __init__(self, kind: str, detail: str, *,
                 status: int | None = None, node_id: int | None = None):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.status = status
        self.node_id = node_id


# ---------------------------------------------------------------------------
# HTTP client.  The collector may only use /open and /search.
# ---------------------------------------------------------------------------

@dataclass
class HttpResult:
    status: int
    body: Any


class Client:
    def __init__(self, base_url: str, timeout: float = 15.0, attempts: int = 3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.attempts = attempts

    def post(self, path: str, payload: dict) -> HttpResult:
        """POST JSON, returning any HTTP status with a decoded JSON body.

        Connection-level failures are retried a few times; a returned error
        status is *not* retried, because it is a contract answer (e.g. 410).
        """
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.attempts):
            req = urllib.request.Request(
                self.base_url + path,
                data=raw,
                method="POST",
                headers={
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return HttpResult(resp.status, _decode(resp.read()))
            except urllib.error.HTTPError as exc:  # still a well-formed answer
                return HttpResult(exc.code, _decode(exc.read()))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
                if attempt + 1 < self.attempts:
                    time.sleep(0.1 * (attempt + 1))
        raise ExtractError("transport", f"{path} unreachable: {last}")


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"_undecodable": str(exc)}


# ---------------------------------------------------------------------------
# Artifact helpers
# ---------------------------------------------------------------------------

def _dump(path: str, obj: Any) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=False)
        fh.write("\n")
    os.replace(tmp, path)


def _load(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ExtractError("missing_artifact", f"{path} not found")
    except json.JSONDecodeError as exc:
        raise ExtractError("bad_artifact", f"{path}: {exc}")


def _echo_ok(echo: Any, request: dict) -> bool:
    if not isinstance(echo, dict):
        return False
    return (
        echo.get("snapshot_id") == request.get("snapshot_id")
        and echo.get("t_range") == request.get("t_range")
        and echo.get("bucket_range") == request.get("bucket_range")
    )


@dataclass
class Node:
    node_id: int
    t_range: list[int]
    bucket_range: list[int]
    parent: int | None = None
    depth: int = 0
    status: str = "pending"  # pending | accepted | saturated | internal | failed
    count: int | None = None
    rows: list[dict] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    error: str | None = None
    note: str | None = None

    def to_json(self) -> dict:
        return {
            "node_id": self.node_id,
            "parent": self.parent,
            "depth": self.depth,
            "t_range": self.t_range,
            "bucket_range": self.bucket_range,
            "status": self.status,
            "count": self.count,
            "rows": self.rows,
            "children": self.children,
            "error": self.error,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Response validation (all failures fatal for this snapshot)
# ---------------------------------------------------------------------------

def _validate_search_response(
    body: Any, request: dict, node: Node, cap: int, domain_t: list[int], domain_b: list[int]
) -> list[dict]:
    """Check the full /search contract.  Returns the accepted rows."""
    if not isinstance(body, dict):
        raise ExtractError("bad_response", f"node {node.node_id}: body is not an object")
    if not _echo_ok(body, request):
        got = {
            "snapshot_id": body.get("snapshot_id"),
            "t_range": body.get("t_range"),
            "bucket_range": body.get("bucket_range"),
        }
        raise ExtractError(
            "echo_mismatch",
            f"node {node.node_id}: requested {request} but echoed {got}",
        )
    count, echoed_cap = body.get("count"), body.get("cap")
    if type(count) is not int or count < 0:
        raise ExtractError("bad_count", f"node {node.node_id}: count={count!r}")
    if type(echoed_cap) is not int or echoed_cap != cap:
        raise ExtractError(
            "bad_cap", f"node {node.node_id}: cap={echoed_cap!r}, expected {cap}"
        )
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise ExtractError("bad_rows", f"node {node.node_id}: rows is not a list")
    if count <= cap:
        # This response is going to be accepted as a complete leaf, so it must
        # carry every row in the region.
        if len(rows) != count:
            raise ExtractError(
                "bad_row_count",
                f"node {node.node_id}: count={count} <= cap={cap} but "
                f"{len(rows)} rows returned",
            )
    elif len(rows) > cap:
        raise ExtractError(
            "bad_row_count",
            f"node {node.node_id}: {len(rows)} rows exceeds cap={cap}",
        )

    seen: set[str] = set()
    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ExtractError(
                "bad_row_type", f"node {node.node_id}: row {idx} is not an object"
            )
        # Same coordinates may legitimately hold several different record_ids,
        # so duplicates are only forbidden per record_id, never per coordinate.
        for name, typ in FIELD_DEFS:
            if name not in row:
                raise ExtractError(
                    "missing_field", f"node {node.node_id}: row {idx} lacks {name}"
                )
            if type(row[name]) is not typ:
                raise ExtractError(
                    "bad_field_type",
                    f"node {node.node_id}: row {idx} {name}={row[name]!r} is not {typ.__name__}",
                )
        extra = set(row) - EXPECTED_FIELDS
        if extra:
            raise ExtractError(
                "unexpected_fields", f"node {node.node_id}: row {idx} has {sorted(extra)}"
            )
        rid = row["record_id"]
        if rid in seen:
            raise ExtractError(
                "duplicate_record_id", f"node {node.node_id}: record_id {rid!r} repeated"
            )
        seen.add(rid)
        t, b = row["t"], row["bucket"]
        if not (node.t_range[0] <= t < node.t_range[1]):
            raise ExtractError(
                "row_out_of_range",
                f"node {node.node_id}: record {rid!r} t={t} outside {node.t_range}",
            )
        if not (node.bucket_range[0] <= b < node.bucket_range[1]):
            raise ExtractError(
                "row_out_of_range",
                f"node {node.node_id}: record {rid!r} bucket={b} outside {node.bucket_range}",
            )
        if not (domain_t[0] <= t < domain_t[1] and domain_b[0] <= b < domain_b[1]):
            raise ExtractError(
                "row_out_of_domain",
                f"node {node.node_id}: record {rid!r} at ({t},{b}) outside the domain",
            )
    return rows


def _error_kind(status: int, body: Any) -> str:
    if status == 410:
        return "snapshot_expired"
    if status == 404:
        return "unknown_snapshot"
    if status == 400:
        return "rejected_request"
    if isinstance(body, dict) and "error" in body:
        return f"http_{status}:{body['error']}"
    return f"http_{status}"


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def _split(ranges: tuple[list[int], list[int]]) -> tuple[tuple[list[int], list[int]], tuple[list[int], list[int]]]:
    """Time-major integer midpoint split; returns the two children."""
    tr, br = ranges
    if tr[1] - tr[0] > 1:
        mid = (tr[0] + tr[1]) // 2
        return ([tr[0], mid], list(br)), ([mid, tr[1]], list(br))
    mid = (br[0] + br[1]) // 2
    return (list(tr), [br[0], mid]), (list(tr), [mid, br[1]])


class _Run:
    """One collection attempt over a single frozen snapshot."""

    def __init__(self, client: Client, out_dir: str, snapshot_id: str,
                 open_rec: dict, queries_fh):
        self.client = client
        self.out_dir = out_dir
        self.snapshot_id = snapshot_id
        self.open_rec = open_rec
        self.queries_fh = queries_fh
        self.domain_t: list[int] = open_rec["t_range"]
        self.domain_b: list[int] = open_rec["bucket_range"]
        self.cap: int = open_rec["cap"]
        self.nodes: dict[int, Node] = {}
        self.run_errors: list[dict] = []
        self.query_count = 0
        self.next_id = 0

    # -- bookkeeping --------------------------------------------------------
    def new_node(self, tr, br, parent, depth) -> Node:
        node = Node(node_id=self.next_id, t_range=tr, bucket_range=br,
                    parent=parent, depth=depth)
        self.next_id += 1
        self.nodes[node.node_id] = node
        return node

    def log_query(self, node: Node, request: dict,
                  result: HttpResult | None, err: str | None) -> None:
        rec = {
            "node_id": node.node_id,
            "request": request,
            "http_status": None if result is None else result.status,
            "response": None if result is None else result.body,
        }
        if err:
            rec["error"] = err
        self.queries_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.queries_fh.flush()

    def fail(self, node: Node, exc: ExtractError) -> None:
        node.status, node.error = "failed", str(exc)
        self.run_errors.append({
            "node_id": node.node_id, "kind": exc.kind,
            "status": exc.status, "error": str(exc),
        })

    # -- main loop ----------------------------------------------------------
    def run(self) -> Node:
        root = self.new_node(list(self.domain_t), list(self.domain_b), None, 0)
        # Iterative DFS so wide/deep domains cannot exhaust Python's stack.
        stack = [root]
        while stack:
            node = stack.pop()
            request = {
                "snapshot_id": self.snapshot_id,
                "t_range": list(node.t_range),
                "bucket_range": list(node.bucket_range),
            }
            self.query_count += 1
            try:
                result = self.client.post("/search", request)
            except ExtractError as exc:
                exc.node_id = node.node_id
                self.log_query(node, request, None, str(exc))
                self.fail(node, exc)
                raise
            self.log_query(node, request, result, None)

            if result.status != 200:
                kind = _error_kind(result.status, result.body)
                exc = ExtractError(
                    kind,
                    f"node {node.node_id} {request['t_range']}/"
                    f"{request['bucket_range']}: {kind} {result.body!r}",
                    status=result.status, node_id=node.node_id,
                )
                self.fail(node, exc)
                raise exc

            try:
                rows = _validate_search_response(
                    result.body, request, node, self.cap,
                    self.domain_t, self.domain_b)
            except ExtractError as exc:
                exc.node_id = node.node_id
                self.fail(node, exc)
                raise

            node.count = result.body["count"]
            node.rows = rows

            if node.count <= self.cap:
                # count <= cap proves the returned rows are the entire leaf.
                node.status = "accepted"
                continue

            span_t = node.t_range[1] - node.t_range[0]
            span_b = node.bucket_range[1] - node.bucket_range[0]
            if span_t == 1 and span_b == 1:
                node.status = "saturated"
                node.note = (
                    f"single cell ({node.t_range[0]},{node.bucket_range[0]}) holds "
                    f"{node.count} records > cap {self.cap}; "
                    f"only {len(rows)} retrievable, "
                    f"{node.count - len(rows)} remain unretrieved"
                )
                continue

            (t1, b1), (t2, b2) = _split((node.t_range, node.bucket_range))
            c1 = self.new_node(t1, b1, node.node_id, node.depth + 1)
            c2 = self.new_node(t2, b2, node.node_id, node.depth + 1)
            node.status = "internal"
            node.children = [c1.node_id, c2.node_id]
            # Union is exactly the parent and the halves are disjoint.
            assert (c1.t_range[1] == c2.t_range[0] and c1.bucket_range == c2.bucket_range) or (
                c1.bucket_range[1] == c2.bucket_range[0] and c1.t_range == c2.t_range)
            stack.append(c2)
            stack.append(c1)
        return root

    # -- verification -------------------------------------------------------
    def verify(self, root: Node) -> None:
        for node in self.nodes.values():
            if node.status != "internal":
                continue
            counts = [self.nodes[cid].count for cid in node.children]
            if any(c is None for c in counts):
                raise ExtractError(
                    "incomplete_children",
                    f"node {node.node_id} has a child without a count",
                    node_id=node.node_id)
            if sum(counts) != node.count:
                raise ExtractError(
                    "count_mismatch",
                    f"node {node.node_id}: parent count {node.count} != "
                    f"children sum {sum(counts)}", node_id=node.node_id)

        accepted = [n for n in self.nodes.values() if n.status == "accepted"]
        saturated = [n for n in self.nodes.values() if n.status == "saturated"]
        seen: dict[str, int] = {}
        for leaf in accepted:
            if len(leaf.rows) != leaf.count:
                raise ExtractError(
                    "leaf_size_mismatch",
                    f"node {leaf.node_id}: rows {len(leaf.rows)} != count {leaf.count}",
                    node_id=leaf.node_id)
            for row in leaf.rows:
                rid = row["record_id"]
                if rid in seen:
                    raise ExtractError(
                        "duplicate_across_leaves",
                        f"record_id {rid!r} in nodes {seen[rid]} and {leaf.node_id}",
                        node_id=leaf.node_id)
                seen[rid] = leaf.node_id

        missing = sum(n.count - len(n.rows) for n in saturated)
        total = root.count
        # Every branch here terminates in an accepted or saturated leaf, so the
        # leaf counts must sum to the root count whatever the shape.
        leaf_total = sum(n.count for n in accepted) + sum(n.count for n in saturated)
        if total is not None and leaf_total != total:
            raise ExtractError(
                "coverage_count_mismatch",
                f"root count {total} != accepted {sum(n.count for n in accepted)} "
                f"+ saturated {sum(n.count for n in saturated)}",
                node_id=root.node_id)
        if root.status == "accepted" and len(seen) != total:
            raise ExtractError(
                "domain_count_mismatch",
                f"root count {total} but accepted leaves yielded {len(seen)}",
                node_id=root.node_id)
        self.missing = missing
        self.accepted = accepted
        self.saturated = saturated
        self.ids = seen

    # -- artifacts ----------------------------------------------------------
    def write_artifacts(self, root: Node, fatal: ExtractError | None) -> dict:
        accepted = getattr(self, "accepted", [
            n for n in self.nodes.values() if n.status == "accepted"])
        saturated = getattr(self, "saturated", [
            n for n in self.nodes.values() if n.status == "saturated"])
        if "ids" not in self.__dict__:
            # Fatal run that never reached verify(): rebuild leaf bookkeeping
            # from what was actually obtained.
            ids: dict[str, int] = {}
            for leaf in accepted:
                for row in leaf.rows:
                    ids.setdefault(row["record_id"], leaf.node_id)
            self.ids = ids
        ids = self.ids
        if "missing" not in self.__dict__:
            self.missing = sum(n.count - len(n.rows) for n in saturated)
        complete = fatal is None and not saturated
        if complete:
            assert root.count == len(ids), (
                f"complete run has root count {root.count} but {len(ids)} rows")
            assert all(n.count == len(n.rows) for n in accepted)

        try:
            self.queries_fh.close()
        except Exception:
            pass

        all_rows = sorted(
            (row for leaf in accepted for row in leaf.rows),
            key=lambda r: r["record_id"])
        jsonl_path = os.path.join(self.out_dir, "records.jsonl")
        partial_path = jsonl_path + ".partial"
        tmp_path = jsonl_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for row in all_rows:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        if complete:
            os.replace(tmp_path, jsonl_path)
            try:
                os.unlink(partial_path)
            except FileNotFoundError:
                pass
        else:
            # Keep what was actually obtained, but never leave the canonical
            # complete-publication path in place.
            os.replace(tmp_path, partial_path)
            try:
                os.unlink(jsonl_path)
            except FileNotFoundError:
                pass

        tree = {
            "snapshot_id": self.snapshot_id,
            "scenario": self.open_rec.get("scenario"),
            "cap": self.cap,
            "domain": {"t_range": self.domain_t, "bucket_range": self.domain_b},
            "root_node": root.node_id,
            "split_strategy": (
                "time-major integer midpoint bisection; bucket only once t is "
                "a single integer; one-cell regions are terminal"
            ),
            "nodes": [self.nodes[i].to_json() for i in sorted(self.nodes)],
        }
        _dump(os.path.join(self.out_dir, "tree.json"), tree)

        saturated_visible = sum(len(n.rows) for n in saturated)
        report = {
            "snapshot_id": self.snapshot_id,
            "scenario": self.open_rec.get("scenario"),
            "cap": self.cap,
            "complete": complete,
            "fatal": None if fatal is None else {
                "kind": fatal.kind, "status": fatal.status,
                "node_id": fatal.node_id,
                "error": str(fatal)},
            "domain": {"t_range": self.domain_t, "bucket_range": self.domain_b},
            "root_count": root.count,
            "accepted_leaf_count": len(accepted),
            "leaf_rows": len(ids),
            "saturated_leaf_count": len(saturated),
            "saturated_visible_rows": saturated_visible,
            "missing_count": self.missing,
            "accounted_count": (None if root.count is None else
                                len(ids) + saturated_visible + self.missing),
            "saturated_ranges": [
                {
                    "node_id": n.node_id,
                    "t_range": n.t_range,
                    "bucket_range": n.bucket_range,
                    "count": n.count,
                    "visible_rows": len(n.rows),
                    "unretrieved": n.count - len(n.rows),
                }
                for n in saturated
            ],
            "query_count": self.query_count,
            "node_count": self.next_id,
            "errors": self.run_errors,
            "artifacts": {
                "tree": "tree.json",
                "queries": "queries.jsonl",
                "records": "records.jsonl" if complete else None,
                "records_partial": None if complete else "records.jsonl.partial",
            },
        }
        _dump(os.path.join(self.out_dir, "coverage.json"), report)
        return report


def collect(
    base_url: str,
    out_dir: str,
    snapshot_id: str,
    timeout: float = 15.0,
    attempts: int = 3,
) -> dict:
    """Partition the whole domain of one open snapshot and persist artifacts.

    A fatal failure (transport, expired snapshot, bad echo, malformed rows,
    count mismatch) still writes the tree, raw queries, partial leaf rows and a
    ``complete: false`` coverage report, then returns that report.  The caller
    must not treat such a report as a complete publication.
    """
    os.makedirs(out_dir, exist_ok=True)
    open_rec = _load(os.path.join(out_dir, "open.json"))
    if open_rec.get("snapshot_id") != snapshot_id:
        raise ExtractError(
            "snapshot_mismatch",
            f"open.json holds {open_rec.get('snapshot_id')!r}, "
            f"asked for {snapshot_id!r}")

    queries_fh = open(os.path.join(out_dir, "queries.jsonl"), "w", encoding="utf-8")
    run = _Run(
        Client(base_url, timeout, attempts), out_dir, snapshot_id, open_rec, queries_fh
    )
    root = None
    fatal: ExtractError | None = None
    try:
        root = run.run()
        run.verify(root)
    except ExtractError as exc:
        fatal = exc
        root = run.nodes.get(0)
    if root is None:  # pragma: no cover - root is always created first
        root = Node(node_id=0, t_range=list(open_rec["t_range"]),
                    bucket_range=list(open_rec["bucket_range"]))
        run.nodes[0] = root
    return run.write_artifacts(root, fatal)


# ---------------------------------------------------------------------------
# open
# ---------------------------------------------------------------------------

def _normalise_config(cfg: Any, path: str) -> dict:
    if not isinstance(cfg, dict):
        raise ExtractError("bad_config", f"{path}: top level must be an object")
    return cfg


def _check_expected(body: dict, expected: dict, source: str) -> None:
    """Compare /open's answer with an optional task configuration."""
    checks = (("cap", body.get("cap")), ("t_range", body.get("t_range")),
              ("bucket_range", body.get("bucket_range")))
    for key, actual in checks:
        if key in expected and expected[key] != actual:
            raise ExtractError(
                "config_mismatch",
                f"{source}: {key}={expected[key]!r} but /open reported {actual!r}")


def open_snapshot(
    base_url: str, scenario: str, out_dir: str, timeout: float = 15.0,
    attempts: int = 3, expected: dict | None = None, config_path: str = "<config>",
) -> dict:
    """POST /open and persist the snapshot identity and whole domain.

    ``expected`` may carry cap/t_range/bucket_range from a task config; a
    disagreement with the service is a fatal config mismatch, never silently
    accepted.
    """
    os.makedirs(out_dir, exist_ok=True)
    client = Client(base_url, timeout, attempts)
    result = client.post("/open", {"scenario": scenario})
    if result.status != 200:
        raise ExtractError(
            _error_kind(result.status, result.body),
            f"/open scenario={scenario!r} -> HTTP {result.status}: {result.body}",
            status=result.status,
        )
    body = result.body
    if not isinstance(body, dict):
        raise ExtractError("bad_open", f"/open body is not an object: {body!r}")
    sid = body.get("snapshot_id")
    cap = body.get("cap")
    tr, br = body.get("t_range"), body.get("bucket_range")
    if not isinstance(sid, str) or not sid:
        raise ExtractError("bad_open", f"invalid snapshot_id {sid!r}")
    if type(cap) is not int or cap <= 0:
        raise ExtractError("bad_open", f"invalid cap {cap!r}")
    for name, rng in (("t_range", tr), ("bucket_range", br)):
        if (
            not isinstance(rng, list)
            or len(rng) != 2
            or any(type(x) is not int for x in rng)
            or not rng[0] < rng[1]
        ):
            raise ExtractError("bad_open", f"invalid {name} {rng!r}")
    if expected:
        _check_expected(body, expected, config_path)
    record = {
        "snapshot_id": sid,
        "scenario": body.get("scenario", scenario),
        "cap": cap,
        "t_range": tr,
        "bucket_range": br,
        "opened_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _dump(os.path.join(out_dir, "open.json"), record)
    return record


# ---------------------------------------------------------------------------
# inspect: structural re-audit of saved artifacts, no server access
# ---------------------------------------------------------------------------

def _covers(parent: list[int], kids: list[list[int]]) -> bool:
    """True if the kids are disjoint and their union is exactly parent."""
    if not kids:
        return False
    kids = sorted(kids)
    if kids[0][0] != parent[0] or kids[-1][1] != parent[1]:
        return False
    for a, b in zip(kids, kids[1:]):
        if a[1] != b[0]:
            return False
    return all(lo < hi for lo, hi in kids)


def _overlaps(a: dict, b: dict) -> bool:
    return (
        a["t_range"][0] < b["t_range"][1]
        and b["t_range"][0] < a["t_range"][1]
        and a["bucket_range"][0] < b["bucket_range"][1]
        and b["bucket_range"][0] < a["bucket_range"][1]
    )


def inspect(out_dir: str) -> dict:
    """Re-check the saved split tree, coverage and published rows.

    Distinguishes a *declared* incomplete run (saturated cells or a fatal
    failure, coverage.complete=false) from a corrupted complete one.  Either
    way any structural violation is reported in ``problems`` and makes
    ``ok`` false.
    """
    tree = _load(os.path.join(out_dir, "tree.json"))
    coverage = _load(os.path.join(out_dir, "coverage.json"))
    problems: list[str] = []
    nodes = {n["node_id"]: n for n in tree["nodes"]}
    if not nodes:
        raise ExtractError("bad_tree", "tree has no nodes")
    root = nodes[tree["root_node"]]
    cap = tree["cap"]
    domain = tree["domain"]
    declared_complete = coverage.get("complete") is True

    if root["t_range"] != domain["t_range"] or root["bucket_range"] != domain["bucket_range"]:
        problems.append("root range does not equal the configured whole domain")
    if root["status"] not in ("accepted", "internal", "failed"):
        problems.append(f"root has unexpected status {root['status']!r}")

    terminal: list[dict] = []
    open_nodes: list[dict] = []
    for nid, node in nodes.items():
        tr, br = node["t_range"], node["bucket_range"]
        for rng in (tr, br):
            if (
                not isinstance(rng, list) or len(rng) != 2
                or any(type(x) is not int for x in rng) or rng[0] >= rng[1]
            ):
                problems.append(f"node {nid}: invalid range {rng!r}")
                break
        if not (domain["t_range"][0] <= tr[0] < tr[1] <= domain["t_range"][1]
                and domain["bucket_range"][0] <= br[0] < br[1] <= domain["bucket_range"][1]):
            problems.append(f"node {nid}: range outside the whole domain")

        status = node["status"]
        if status == "internal":
            kids = [nodes[c] for c in node["children"]]
            if len(kids) != 2:
                problems.append(f"node {nid}: internal node has {len(kids)} children")
                continue
            # A failure aborts the run, so a declared-incomplete snapshot may
            # legitimately hold an internal node whose children were never
            # queried.  Its isolation problem is fatal_count_mismatch, reported
            # in ``fatal`` rather than here.
            unresolved = any(k["count"] is None for k in kids)
            if unresolved:
                if declared_complete:
                    problems.append(f"node {nid}: unresolved children in a complete run")
            elif node["count"] != sum(k["count"] for k in kids):
                # If the recorded fatal cause *is* a count mismatch, this
                # inconsistency is the server lie the run already refused to
                # publish, not a defect of the split tree.
                fatal = coverage.get("fatal") or {}
                if not (fatal.get("kind") in ("count_mismatch", "coverage_count_mismatch")
                        and fatal.get("node_id") == nid):
                    problems.append(
                        f"node {nid}: count {node['count']} != children sum "
                        f"{sum(k['count'] for k in kids)}")
            # Structure (which dimension was split, how it was partitioned)
            # must be valid regardless of whether the run is complete.
            same_t = all(k["t_range"] == tr for k in kids)
            same_b = all(k["bucket_range"] == br for k in kids)
            if same_t and not same_b:
                if br[1] - br[0] <= 1:
                    problems.append(f"node {nid}: split a single-value bucket range")
                elif not _covers(br, [k["bucket_range"] for k in kids]):
                    problems.append(f"node {nid}: bucket children do not partition parent")
            elif same_b and not same_t:
                if tr[1] - tr[0] <= 1:
                    problems.append(f"node {nid}: split a single-value t range")
                elif not _covers(tr, [k["t_range"] for k in kids]):
                    problems.append(f"node {nid}: t children do not partition parent")
            else:
                problems.append(
                    f"node {nid}: children neither keep t fixed nor keep bucket fixed")
            for k in kids:
                if k["parent"] != nid:
                    problems.append(f"node {k['node_id']}: parent pointer != {nid}")
        elif status == "accepted":
            if node["count"] > cap:
                problems.append(f"node {nid}: accepted count {node['count']} > cap {cap}")
            if len(node["rows"]) != node["count"]:
                problems.append(
                    f"node {nid}: accepted rows {len(node['rows'])} != count {node['count']}")
            if node["children"]:
                problems.append(f"node {nid}: accepted leaf has children")
            terminal.append(node)
        elif status == "saturated":
            if node["count"] <= cap:
                problems.append(f"node {nid}: saturated but count {node['count']} <= cap")
            if tr[1] - tr[0] != 1 or br[1] - br[0] != 1:
                problems.append(f"node {nid}: saturated but still divisible {tr}/{br}")
            if len(node["rows"]) != min(node["count"], cap):
                problems.append(f"node {nid}: saturated payload does not match cap")
            if node["children"]:
                problems.append(f"node {nid}: saturated leaf has children")
            terminal.append(node)
        else:
            # failed / pending: an open branch kept for diagnostics.  Only
            # legitimate when the run is declared incomplete.
            open_nodes.append(node)

    fatal = coverage.get("fatal")
    if open_nodes and declared_complete:
        problems.append(
            f"complete run has {len(open_nodes)} failed/pending nodes: "
            f"{[n['node_id'] for n in open_nodes]}")
    if fatal is None and open_nodes:
        problems.append("failed/pending nodes without a recorded fatal cause")

    # Disjointness of every terminal leaf, and full coverage when complete.
    for i, a in enumerate(terminal):
        for b in terminal[i + 1:]:
            if _overlaps(a, b):
                problems.append(
                    f"leaves {a['node_id']} and {b['node_id']} overlap")
    area = (domain["t_range"][1] - domain["t_range"][0]) * (
        domain["bucket_range"][1] - domain["bucket_range"][0])
    covered = sum(
        (n["t_range"][1] - n["t_range"][0]) *
        (n["bucket_range"][1] - n["bucket_range"][0]) for n in terminal)
    if declared_complete and covered != area:
        problems.append(f"leaf area {covered} != domain area {area}")

    # Leaf bookkeeping: totals, unique ids, row positions.
    ids: dict[str, int] = {}
    accepted_rows = 0
    accepted_leaves = [n for n in terminal if n["status"] == "accepted"]
    saturated = [n for n in terminal if n["status"] == "saturated"]
    for n in accepted_leaves:
        for row in n["rows"]:
            accepted_rows += 1
            rid = row["record_id"]
            if rid in ids:
                problems.append(
                    f"record_id {rid!r} duplicated in nodes {ids[rid]} and {n['node_id']}")
            ids[rid] = n["node_id"]
            if not (n["t_range"][0] <= row["t"] < n["t_range"][1]
                    and n["bucket_range"][0] <= row["bucket"] < n["bucket_range"][1]):
                problems.append(f"record {rid!r} outside its leaf range")
    expected_accepted = sum(n["count"] for n in accepted_leaves)
    if accepted_rows != expected_accepted:
        problems.append(
            f"accepted leaves hold {accepted_rows} rows but promise {expected_accepted}")

    if declared_complete:
        if saturated:
            problems.append("complete run contains saturated leaves")
        if root["count"] != len(ids):
            problems.append(
                f"complete run: root count {root['count']} != {len(ids)} accepted ids")
        leaf_total = sum(n["count"] for n in accepted_leaves)
        if leaf_total != root["count"]:
            problems.append(
                f"sum of accepted leaf counts {leaf_total} != root count {root['count']}")
    elif fatal is None:
        # Saturated-only (no abort): the retrieved + missing accounting must
        # still explain the whole root count.  With a fatal abort, part of the
        # domain was never queried and the shortfall is the failure itself.
        accounted = accepted_rows + sum(len(n["rows"]) for n in saturated) + \
            sum(n["count"] - len(n["rows"]) for n in saturated)
        if not open_nodes and root["count"] != accounted:
            problems.append(
                f"declared-incomplete but accepted+saturated counts {accounted} != "
                f"root count {root['count']}")

    # Published JSONL: only a complete run may have records.jsonl, and an
    # incomplete run must retain its partial leaves.
    record_path = os.path.join(out_dir, "records.jsonl")
    partial_path = os.path.join(out_dir, "records.jsonl.partial")
    jsonl_rows: list[dict] = []
    if os.path.exists(record_path):
        if not declared_complete:
            problems.append("incomplete run must not publish records.jsonl")
        with open(record_path, encoding="utf-8") as fh:
            jsonl_rows = [json.loads(line) for line in fh if line.strip()]
        if len(jsonl_rows) != accepted_rows:
            problems.append(
                f"records.jsonl has {len(jsonl_rows)} rows, leaves yield {accepted_rows}")
        keys = [r["record_id"] for r in jsonl_rows]
        if keys != sorted(keys):
            problems.append("records.jsonl is not sorted by record_id")
        if len(set(keys)) != len(keys):
            problems.append("records.jsonl repeats a record_id")
    elif declared_complete:
        problems.append("complete run has no records.jsonl")
    elif accepted_rows > 0 and not os.path.exists(partial_path):
        problems.append("incomplete run lost its partial rows")

    if coverage.get("snapshot_id") != tree.get("snapshot_id"):
        problems.append("coverage/tree snapshot mismatch")
    if (coverage.get("root_count") != root["count"]
            or coverage.get("leaf_rows") != len(ids)
            or coverage.get("saturated_leaf_count") != len(saturated)
            or coverage.get("accepted_leaf_count") != len(accepted_leaves)):
        problems.append("coverage counters disagree with the persisted tree")

    return {
        "snapshot_id": tree.get("snapshot_id"),
        "declared_complete": declared_complete,
        "audited_complete": declared_complete and not problems,
        "nodes": len(nodes),
        "accepted_leaves": len(accepted_leaves),
        "saturated_leaves": len(saturated),
        "open_failed_nodes": len(open_nodes),
        "accepted_rows": accepted_rows,
        "retrieved_ids": len(ids),
        "saturated_visible_rows": sum(len(n["rows"]) for n in saturated),
        "unretrieved_rows": sum(n["count"] - len(n["rows"]) for n in saturated),
        "jsonl_rows": len(jsonl_rows),
        "problems": problems,
        "ok": not problems,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extract.py",
        description="Complete extraction through a capped, frozen /open + /search endpoint.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_open = sub.add_parser("open", help="open one snapshot and record its identity/domain")
    p_open.add_argument("--config", help="task config JSON (base_url/scenario/out_dir/cap/ranges)")
    p_open.add_argument("--base-url")
    p_open.add_argument("--scenario")
    p_open.add_argument("--out-dir")
    p_open.add_argument("--timeout", type=float, default=15.0)
    p_open.add_argument("--attempts", type=int, default=3)

    p_collect = sub.add_parser("collect", help="partition the whole frozen snapshot")
    p_collect.add_argument("--config", help="task config JSON (base_url/out_dir)")
    p_collect.add_argument("--base-url")
    p_collect.add_argument("--out-dir")
    p_collect.add_argument("--snapshot-id", help="defaults to the id in OUT_DIR/open.json")
    p_collect.add_argument("--timeout", type=float, default=15.0)
    p_collect.add_argument("--attempts", type=int, default=3)

    p_inspect = sub.add_parser("inspect", help="re-audit saved artifacts without network")
    p_inspect.add_argument("--out-dir")
    p_inspect.add_argument("--config", help="task config JSON (out_dir)")

    return parser


def _merge(args, config: dict) -> tuple[str | None, str | None, str | None]:
    """Resolve base_url / scenario / out_dir, CLI winning over the config."""
    base_url = getattr(args, "base_url", None) or config.get("base_url")
    scenario = getattr(args, "scenario", None) or config.get("scenario")
    out_dir = getattr(args, "out_dir", None) or config.get("out_dir")
    return base_url, scenario, out_dir


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config: dict = {}
    try:
        if getattr(args, "config", None):
            config = _normalise_config(_load(args.config), args.config)

        if args.command == "open":
            base_url, scenario, out_dir = _merge(args, config)
            for name, value in (("--base-url", base_url), ("--scenario", scenario),
                                ("--out-dir", out_dir)):
                if not value:
                    raise ExtractError("usage", f"{name} is required (or set it in --config)")
            expected = {k: config[k] for k in ("cap", "t_range", "bucket_range")
                        if k in config}
            rec = open_snapshot(
                base_url, scenario, out_dir, args.timeout, args.attempts,
                expected=expected, config_path=args.config or "<config>"
            )
            print(
                json.dumps(
                    {
                        "snapshot_id": rec["snapshot_id"],
                        "scenario": rec["scenario"],
                        "cap": rec["cap"],
                        "t_range": rec["t_range"],
                        "bucket_range": rec["bucket_range"],
                        "out_dir": os.path.abspath(out_dir),
                    }
                )
            )
            return EXIT_OK

        if args.command == "collect":
            base_url, _, out_dir = _merge(args, config)
            for name, value in (("--base-url", base_url), ("--out-dir", out_dir)):
                if not value:
                    raise ExtractError("usage", f"{name} is required (or set it in --config)")
            snapshot_id = args.snapshot_id
            if snapshot_id is None:
                snapshot_id = _load(os.path.join(out_dir, "open.json"))["snapshot_id"]
            report = collect(
                base_url, out_dir, snapshot_id, args.timeout, args.attempts
            )
            print(json.dumps({
                "snapshot_id": report["snapshot_id"],
                "complete": report["complete"],
                "root_count": report["root_count"],
                "leaf_rows": report["leaf_rows"],
                "missing_count": report["missing_count"],
                "accepted_leaf_count": report["accepted_leaf_count"],
                "saturated_leaf_count": report["saturated_leaf_count"],
                "query_count": report["query_count"],
                "out_dir": os.path.abspath(out_dir),
            }))
            return EXIT_OK if report["complete"] else EXIT_INCOMPLETE

        if args.command == "inspect":
            out_dir = args.out_dir or config.get("out_dir")
            if not out_dir:
                raise ExtractError("usage", "--out-dir is required (or set it in --config)")
            audit = inspect(out_dir)
            print(json.dumps(audit, ensure_ascii=False))
            return EXIT_OK if audit["ok"] else EXIT_INCOMPLETE
    except ExtractError as exc:
        print(json.dumps({"ok": False, "error": str(exc), "kind": exc.kind, "status": exc.status}), file=sys.stderr)
        return EXIT_INCOMPLETE if exc.kind not in ("usage",) else EXIT_USAGE
    return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
