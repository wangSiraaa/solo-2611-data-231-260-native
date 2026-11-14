#!/usr/bin/env python3
"""Acceptance harness.

The client itself never reads rows.json or /__inspect; this harness may,
because its only job is to verify the client against ground truth and to
inspect server-side state. It checks:

  1. every fixture query matches an independent full sort of rows.json;
  2. results are identical for page_size 1..3 (same query, same hits);
  3. the client is deterministic: two runs give identical hits and logs;
  4. top2 stops before either index is fully read, with fewer scan items
     than two full index scans (the reduction is in actual interface items);
  5. ties (b/c/d, and e/f under w=1/2) are arbitrated by id byte order;
  6. k > object count returns all objects;
  7. edge cases: empty data and a single object;
  8. snapshot invalidation: with --change-after the query fails and no
     success result is published.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from query_client import (IndexClient, QueryError, SnapshotChangedError,  # noqa: E402
                          run_topk)

SERVER = ROOT / 'index_server.py'
ROWS = ROOT / 'fixtures' / 'rows.json'
QUERIES = ROOT / 'fixtures' / 'queries.json'

FAILURES = []


def check(name, cond, detail=''):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f'  ({detail})' if detail else ''))
    if not cond:
        FAILURES.append(name)


def start_server(rows=ROWS, extra=()):
    fd, ready_path = tempfile.mkstemp(suffix='.json')
    pathlib.Path(ready_path).unlink()  # server must create it
    proc = subprocess.Popen(
        [sys.executable, str(SERVER), '--rows', str(rows),
         '--ready', ready_path, *map(str, extra)])
    ready = pathlib.Path(ready_path)
    for _ in range(200):
        if ready.exists() and ready.stat().st_size:
            break
        if proc.poll() is not None:
            raise RuntimeError('server exited early')
        time.sleep(0.05)
    base = json.loads(ready.read_text())['base_url']
    return proc, base


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=10) as r:
        return json.loads(r.read())


def ground_truth(k, wx, wy, rows_path=ROWS):
    """Independent full sort of the raw data -- acceptance use only."""
    rows = json.loads(pathlib.Path(rows_path).read_text())
    ranked = sorted(rows, key=lambda r: (-(wx * r['x'] + wy * r['y']), r['id'].encode()))
    return [{'id': r['id'], 'x': r['x'], 'y': r['y'],
             'total': wx * r['x'] + wy * r['y']} for r in ranked[:k]]


def hit_tuples(rep):
    return [(h['id'], h['x'], h['y'], h['total']) for h in rep['hits']]


def main():
    queries = json.loads(QUERIES.read_text())
    proc, base = start_server()
    try:
        # 1. fixture queries match the independent full sort
        for q in queries:
            rep = run_topk(IndexClient(base), q['k'], q['weights'], q['page_size'])
            exp = ground_truth(q['k'], q['weights']['x'], q['weights']['y'])
            got = hit_tuples(rep)
            want = [(h['id'], h['x'], h['y'], h['total']) for h in exp]
            check(f"fixture {q['id']}: hits == full sort", got == want,
                  f'got={got} want={want}' if got != want else
                  f"{len(got)} hits, stop={rep['stop']['reason']}")

        # 2. page-size sweep: hits identical for page_size 1..3
        for q in queries:
            runs = [run_topk(IndexClient(base), q['k'], q['weights'], ps)
                    for ps in (1, 2, 3)]
            same = all(hit_tuples(r) == hit_tuples(runs[0]) for r in runs[1:])
            check(f"{q['id']}: page_size 1/2/3 give identical hits", same)

        # 3. determinism: identical hits and identical request logs
        q = queries[0]
        r1 = run_topk(IndexClient(base), q['k'], q['weights'], q['page_size'])
        r2 = run_topk(IndexClient(base), q['k'], q['weights'], q['page_size'])
        check('determinism: two runs identical',
              hit_tuples(r1) == hit_tuples(r2)
              and r1['requests']['log'] == r2['requests']['log'])

        # 4. top2 stops before either index is fully read
        q2 = next(q for q in queries if q['id'] == 'top2')
        rep = run_topk(IndexClient(base), q2['k'], q2['weights'], q2['page_size'])
        n = rep['object_count']
        fr = rep['stop']['frontiers']
        check('top2: stop reason is threshold', rep['stop']['reason'] == 'threshold',
              rep['stop']['basis'])
        check('top2: neither index fully read',
              not fr['x']['exhausted'] and not fr['y']['exhausted']
              and fr['x']['cursor'] < n and fr['y']['cursor'] < n,
              f"x={fr['x']['cursor']}/{n} y={fr['y']['cursor']}/{n}")
        scan_items = rep['requests']['scan_items_returned']
        check('top2: fewer scan items than two full index scans',
              scan_items < rep['requests']['full_scan_items'],
              f"{scan_items} < {rep['requests']['full_scan_items']}")

        # 5. tie arbitration by id byte order
        rep = run_topk(IndexClient(base), 4, {'x': 3, 'y': 1}, 2)
        ids = [h['id'] for h in rep['hits']]
        check('ties b/c/d ordered by id', ids == ['a', 'b', 'c', 'd'], str(ids))
        rep = run_topk(IndexClient(base), 20, {'x': 1, 'y': 2}, 3)
        ids = [h['id'] for h in rep['hits']]
        check('k>count returns all, e before f on equal total',
              ids == ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'], str(ids))
        ef = {h['id']: h['total'] for h in rep['hits'] if h['id'] in 'ef'}
        check('e and f actually tie on total', ef.get('e') == ef.get('f'), str(ef))
    finally:
        proc.terminate()

    # 7. edge cases: empty data and a single object
    for name, rows in [('empty', []), ('single', [{'id': 'z', 'x': 5, 'y': 7}])]:
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(rows, f)
            path = f.name
        proc, base = start_server(rows=path)
        try:
            rep = run_topk(IndexClient(base), 3, {'x': 1, 'y': 1}, 2)
            want = [(r['id'], r['x'], r['y'], r['x'] + r['y']) for r in rows]
            check(f'edge {name}: hits == full sort', hit_tuples(rep) == want,
                  f'got={hit_tuples(rep)}')
            check(f'edge {name}: clean stop', rep['status'] == 'ok'
                  and rep['stop']['reason'] in ('index_exhausted', 'all_objects_seen'),
                  rep['stop']['reason'])
        finally:
            proc.terminate()

    # 8. snapshot invalidation: no success result may be published
    proc, base = start_server(extra=('--change-after', 2))
    try:
        client = IndexClient(base)  # pins snapshot-1
        failed, rep = False, None
        try:
            rep = run_topk(client, 2, {'x': 3, 'y': 1}, 2)
        except SnapshotChangedError:
            failed = True
        except QueryError:
            failed = True
        check('change-after: query fails on snapshot change', failed)
        check('change-after: no result published', rep is None)
        insp = get(base, '/__inspect')  # acceptance-only check of server state
        check('change-after: server really switched snapshot',
              insp['snapshot'] == 'snapshot-2'
              and len(insp['calls']) == 2, str(insp))
    finally:
        proc.terminate()

    print()
    if FAILURES:
        print(f'{len(FAILURES)} FAILURE(S): {FAILURES}')
        return 1
    print('ALL ACCEPTANCE CHECKS PASSED')
    return 0


if __name__ == '__main__':
    sys.exit(main())
