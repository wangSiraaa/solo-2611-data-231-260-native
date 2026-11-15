#!/usr/bin/env python3
"""Persistent session bookmarks and read routing for a primary/replica SQLite pair.

Guarantee (per client): read-your-writes. A client never reads a snapshot older
than the commit receipts it observed itself. Everything else is explicitly NOT
guaranteed: no global strong consistency, no promise of reading the latest data.

Components
----------
- observe(client_id, receipt): validate a commit receipt against cluster.json
  and advance the client's persistent bookmark (max wins, never regresses).
- read_report(client_id, wait_ms, fallback): replica-first read routing. The
  identity check (cluster_id/generation) and the applied_seq >= bookmark check
  happen inside the very same BEGIN read transaction that runs the two report
  queries from read-report.json, so meta, accounts and history always come from
  one snapshot.
- CLI: see `python3 router.py --help`.

Data root layout (created by replica_fixture.py init):
    <root>/cluster.json   expected identity {"cluster_id": ..., "generation": ...}
    <root>/primary.db     primary node
    <root>/replica.db     replica node
    <root>/bookmarks.db   persistent per-client bookmarks (created by this module)
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
DEFAULT_REPORT_SPEC = HERE / 'fixtures' / 'read-report.json'
RETRY_INTERVAL_S = 0.02

BOOKMARKS_DDL = (
    'CREATE TABLE IF NOT EXISTS bookmarks('
    'client_id TEXT PRIMARY KEY,'
    'commit_seq INTEGER NOT NULL CHECK(commit_seq >= 0))'
)


class RouterError(Exception):
    """Expected, user-reportable failure (usage, validation, environment)."""

    def __init__(self, err_type: str, detail: str, **extra):
        super().__init__(f'{err_type}: {detail}')
        self.err_type = err_type
        self.detail = detail
        self.extra = extra


@dataclass
class Hooks:
    """Test hooks fired from inside read_report's genuine control flow.

    They let a test inject events at precise interleaving points WITHOUT
    bypassing read_report:

    after_node_selected(node): after a node is chosen for an attempt, before
        its BEGIN. E.g. reset the node's generation here; the identity check
        that follows runs on the real snapshot and must catch it.
    after_accounts(): inside the open read transaction, between the accounts
        query and the history query. E.g. commit + replicate here; the history
        query must still see the old snapshot.
    """
    after_node_selected: Optional[Callable[[str], None]] = None
    after_accounts: Optional[Callable[[], None]] = None


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------

def _is_int(x) -> bool:
    # bool is a subclass of int; receipts/booleans must not be accepted as seqs.
    return isinstance(x, int) and not isinstance(x, bool)


def _validate_client_id(client_id) -> None:
    if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 128:
        raise RouterError('bad_client_id', 'client_id must be a non-empty string (<=128 chars)')


def load_expected_identity(root: Path) -> dict:
    """The expected cluster_id/generation comes from cluster.json (created by init)."""
    path = root / 'cluster.json'
    if not path.exists():
        raise RouterError('not_initialized',
                          f'{path} not found; run: python3 replica_fixture.py --root {root} init')
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RouterError('bad_cluster_file', f'{path} is not valid JSON: {e}')
    cid, gen = data.get('cluster_id'), data.get('generation')
    if not isinstance(cid, str) or not cid or not _is_int(gen):
        raise RouterError('bad_cluster_file',
                          f'{path} must contain cluster_id (non-empty str) and generation (int)')
    return {'cluster_id': cid, 'generation': gen}


def _load_report_spec(report_path: Optional[Path]) -> dict:
    path = Path(report_path) if report_path else DEFAULT_REPORT_SPEC
    if not path.exists():
        raise RouterError('bad_report_spec', f'{path} not found')
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise RouterError('bad_report_spec', f'{path} is not valid JSON: {e}')
    spec = {}
    for key in ('accounts_query', 'history_query'):
        q = data.get(key)
        if not isinstance(q, str) or not q.strip().upper().startswith('SELECT'):
            raise RouterError('bad_report_spec', f'{path}: {key} must be a SELECT string')
        spec[key] = q
    return spec


# --------------------------------------------------------------------------
# persistent bookmarks
# --------------------------------------------------------------------------

def _bookmarks_conn(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(root / 'bookmarks.db', timeout=5)
    conn.isolation_level = None  # autocommit; explicit BEGIN where needed
    conn.execute(BOOKMARKS_DDL)
    return conn


def get_bookmark(root: Path, client_id: str) -> int:
    """A client that never observed a receipt starts at seq 0."""
    _validate_client_id(client_id)
    conn = _bookmarks_conn(root)
    try:
        row = conn.execute('SELECT commit_seq FROM bookmarks WHERE client_id=?',
                           (client_id,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def observe(root: Path, client_id: str, receipt: dict) -> dict:
    """Validate a commit receipt and advance the client's bookmark.

    Only receipts for this cluster's current generation with a non-negative
    integer commit_seq are accepted. Wrong cluster/generation is an explicit
    error and never touches the stored bookmark. The bookmark is the max of
    observed seqs: duplicates and out-of-order receipts cannot regress it.
    Bookmarks are per client and persist across restarts.
    """
    expected = load_expected_identity(root)
    _validate_client_id(client_id)
    if not isinstance(receipt, dict):
        raise RouterError('bad_receipt',
                          'receipt must be a JSON object with cluster_id, generation, commit_seq')
    missing = [k for k in ('cluster_id', 'generation', 'commit_seq') if k not in receipt]
    if missing:
        raise RouterError('bad_receipt', f'receipt missing field(s): {missing}')
    cid, gen, seq = receipt['cluster_id'], receipt['generation'], receipt['commit_seq']
    if cid != expected['cluster_id']:
        raise RouterError('cluster_mismatch',
                          f"receipt cluster_id {cid!r} != expected {expected['cluster_id']!r}; "
                          'bookmark left untouched')
    if not _is_int(gen) or gen != expected['generation']:
        raise RouterError('generation_mismatch',
                          f"receipt generation {gen!r} != expected {expected['generation']!r}; "
                          'bookmark left untouched')
    if not _is_int(seq) or seq < 0:
        raise RouterError('invalid_seq',
                          f'commit_seq must be a non-negative integer, got {seq!r}')

    conn = _bookmarks_conn(root)
    try:
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT commit_seq FROM bookmarks WHERE client_id=?',
                           (client_id,)).fetchone()
        previous = row[0] if row else 0
        bookmark = max(previous, seq)  # never regresses
        conn.execute(
            'INSERT INTO bookmarks(client_id, commit_seq) VALUES(?, ?)'
            ' ON CONFLICT(client_id) DO UPDATE SET commit_seq=excluded.commit_seq',
            (client_id, bookmark))
        conn.execute('COMMIT')
    finally:
        conn.close()
    return {'status': 'ok', 'client_id': client_id, 'receipt_seq': seq,
            'previous_bookmark': previous, 'bookmark': bookmark,
            'advanced': bookmark > previous}


# --------------------------------------------------------------------------
# read routing
# --------------------------------------------------------------------------

def _query_dicts(conn: sqlite3.Connection, sql: str) -> list:
    cur = conn.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _attempt(root: Path, node: str, expected: dict, required: int,
             spec: dict, hooks: Optional[Hooks]) -> dict:
    """One full read attempt on `node`.

    Everything — identity check, applied_seq check, accounts query, history
    query — runs inside ONE BEGIN read transaction, i.e. one snapshot. There is
    deliberately no separate "check progress, then open another transaction"
    path. The snapshot is always closed (ROLLBACK/COMMIT + close) before this
    function returns, so a retry never sits on a stale snapshot.
    """
    if hooks and hooks.after_node_selected:
        hooks.after_node_selected(node)
    db = root / f'{node}.db'
    if not db.exists():
        raise RouterError('node_unavailable', f'{db} not found; run replica_fixture.py init')
    conn = sqlite3.connect(db, timeout=5)
    conn.isolation_level = None  # explicit transaction control
    try:
        conn.execute('BEGIN')  # deferred read txn; the first SELECT pins the snapshot
        try:
            row = conn.execute(
                'SELECT cluster_id, generation, applied_seq FROM meta').fetchone()
        except sqlite3.Error as e:
            raise RouterError('node_error', f'{node}: cannot read meta: {e}')
        if row is None:
            raise RouterError('node_error', f'{node}: meta table is empty')
        meta = {'cluster_id': row[0], 'generation': row[1], 'applied_seq': row[2]}

        # Identity check on the real snapshot. A mismatch is an explicit error
        # outcome, not ordinary lag: the caller must not silently fall back.
        if (meta['cluster_id'] != expected['cluster_id']
                or meta['generation'] != expected['generation']):
            conn.execute('ROLLBACK')
            return {'kind': 'identity_mismatch', 'meta': meta}

        # Progress check on the same snapshot.
        if meta['applied_seq'] < required:
            conn.execute('ROLLBACK')
            return {'kind': 'not_ready', 'meta': meta}

        accounts = _query_dicts(conn, spec['accounts_query'])
        if hooks and hooks.after_accounts:
            hooks.after_accounts()  # mid-transaction writes must not leak in
        history_rows = _query_dicts(conn, spec['history_query'])
        conn.execute('COMMIT')
        history = history_rows[0] if len(history_rows) == 1 else history_rows
        return {'kind': 'ok', 'meta': meta, 'accounts': accounts, 'history': history}
    finally:
        conn.close()


def read_report(root: Path, client_id: str, wait_ms: int, fallback: str,
                hooks: Optional[Hooks] = None,
                report_path: Optional[Path] = None) -> dict:
    """Read the report for a client, replica first, honoring its bookmark.

    Retries the replica with fresh snapshots (monotonic clock, wait_ms budget).
    On exhaustion: fallback='primary' re-opens and validates a snapshot on the
    primary; fallback='none' returns an explicit not_ready (never stale data).
    identity_mismatch on any node is returned immediately, with no implicit
    fallback. Returns a JSON-serializable dict containing: status, selected
    node, required_seq (the client's bookmark), snapshot_meta actually read,
    accounts/history results, and the attempt count.
    """
    if not _is_int(wait_ms) or wait_ms < 0:
        raise RouterError('bad_argument', 'wait_ms must be a non-negative integer')
    if fallback not in ('primary', 'none'):
        raise RouterError('bad_argument', "fallback must be 'primary' or 'none'")
    _validate_client_id(client_id)
    expected = load_expected_identity(root)
    spec = _load_report_spec(report_path)
    required = get_bookmark(root, client_id)  # fixed for the duration of this read

    start = time.monotonic()
    deadline = start + wait_ms / 1000.0
    attempts = 0
    observed = {}  # node -> last snapshot meta seen there

    def base() -> dict:
        return {'client_id': client_id,
                'required_seq': required,
                'expected_identity': expected,
                'wait_ms': wait_ms,
                'fallback': fallback,
                'attempts': attempts,
                'elapsed_ms': round((time.monotonic() - start) * 1000, 3)}

    def finish(outcome: dict, node: str) -> dict:
        meta = outcome['meta']
        observed[node] = meta
        if outcome['kind'] == 'identity_mismatch':
            return {**base(), 'status': 'identity_mismatch', 'node': node,
                    'snapshot_meta': meta,
                    'error': (f"node '{node}' identity {meta['cluster_id']!r}/"
                              f"generation {meta['generation']} != expected "
                              f"{expected['cluster_id']!r}/generation "
                              f"{expected['generation']}; explicit identity_mismatch, "
                              'no implicit fallback')}
        return {**base(), 'status': 'ok', 'node': node, 'snapshot_meta': meta,
                'accounts': outcome['accounts'], 'history': outcome['history']}

    # Phase 1: replica, retrying within the wait budget. Every attempt closes
    # the old snapshot and opens a new one, so newly replicated progress is seen.
    while True:
        attempts += 1
        outcome = _attempt(root, 'replica', expected, required, spec, hooks)
        if outcome['kind'] != 'not_ready':
            return finish(outcome, 'replica')
        observed['replica'] = outcome['meta']
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(RETRY_INTERVAL_S, remaining))

    # Phase 2: wait budget exhausted on the replica.
    if fallback == 'primary':
        attempts += 1
        outcome = _attempt(root, 'primary', expected, required, spec, hooks)
        if outcome['kind'] != 'not_ready':
            return finish(outcome, 'primary')
        observed['primary'] = outcome['meta']

    return {**base(), 'status': 'not_ready', 'node': None, 'observed_meta': observed,
            'error': (f'no node reached required_seq={required} within '
                      f'wait_ms={wait_ms}; explicit not_ready, no stale data returned')}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _load_receipt(args) -> dict:
    if args.receipt is not None and args.receipt_file is not None:
        raise RouterError('bad_argument', 'use --receipt or --receipt-file, not both')
    if args.receipt is not None:
        text = args.receipt
    elif args.receipt_file is not None:
        text = sys.stdin.read() if args.receipt_file == '-' else Path(args.receipt_file).read_text()
    else:
        raise RouterError('bad_argument',
                          'receipt required: --receipt JSON or --receipt-file PATH (- for stdin)')
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise RouterError('bad_receipt', f'receipt is not valid JSON: {e}')


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog='router.py',
        description='Persistent session bookmarks and read routing for a '
                    'primary/replica SQLite pair. Read-your-writes per client; '
                    'no global strong consistency, no promise of latest data.')
    p.add_argument('--root', type=Path, required=True,
                   help='data root with cluster.json, primary.db, replica.db '
                        '(bookmarks.db is created here)')
    p.add_argument('--report', type=Path, default=None,
                   help='path to read-report.json (default: fixtures/read-report.json '
                        'next to router.py)')
    sub = p.add_subparsers(dest='cmd', required=True)

    so = sub.add_parser('observe', help='validate a commit receipt and advance the '
                                        'client bookmark (max wins, never regresses)')
    so.add_argument('--client', required=True)
    so.add_argument('--receipt', help='receipt as a JSON string')
    so.add_argument('--receipt-file', help='path to receipt JSON, or - for stdin')

    sr = sub.add_parser('read', help='read the report, replica first, honoring the '
                                     'client bookmark')
    sr.add_argument('--client', required=True)
    sr.add_argument('--wait-ms', type=int, required=True,
                    help='how long to retry the replica before giving up / falling back')
    sr.add_argument('--fallback', choices=['primary', 'none'], required=True,
                    help='what to do when the replica stays too stale past wait-ms')

    sb = sub.add_parser('bookmark', help='show the stored bookmark for a client')
    sb.add_argument('--client', required=True)

    args = p.parse_args(argv)
    try:
        if args.cmd == 'observe':
            out, code = observe(args.root, args.client, _load_receipt(args)), 0
        elif args.cmd == 'read':
            out = read_report(args.root, args.client, args.wait_ms, args.fallback,
                              report_path=args.report)
            # ok and not_ready are normal routing outcomes; identity_mismatch is an error.
            code = 0 if out['status'] in ('ok', 'not_ready') else 1
        else:  # bookmark
            out = {'status': 'ok', 'client_id': args.client,
                   'bookmark': get_bookmark(args.root, args.client)}
            code = 0
    except RouterError as e:
        out = {'status': 'error', 'error': e.err_type, 'detail': e.detail, **e.extra}
        code = 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return code


if __name__ == '__main__':
    sys.exit(main())
