#!/usr/bin/env python3
"""Exact top-K client over the two-column sorted-index HTTP service.

Contract (fixtures/protocol.md): the server exposes per-column sorted scans
and by-id lookups under a pinned snapshot; it does NOT sort by the weighted
total, and the client must not read rows.json or /__inspect. This client
therefore runs a Fagin-style threshold algorithm (TA):

  * /info pins the snapshot for the whole query. Every request carries it
    and every response is checked against it. Any mismatch fails the query:
    scores from two snapshots are never combined, and there is no silent
    retry that would keep candidates gathered under another snapshot.
  * Round-robin sorted scans advance the x/y indexes one page at a time
    (a fixed deterministic strategy, no cost-based scheduling). Each
    frontier keeps its cursor, the last score it delivered and an
    exhausted flag.
  * The first time an id appears in any scan, one batched /lookup (random
    access) completes its other column and the (x, y) pair is cached.
    Sightings in the other index reuse the cache: never a duplicate
    candidate, never a duplicate lookup.
  * Any object not seen so far sits beyond both scanned prefixes, so its
    total is at most T = wx*last_x + wy*last_y (exact integers). Scanning
    stops only when K complete candidates exist and the K-th one totals
    strictly more than T. On a tie an unseen object with a smaller id
    could still displace it, so scanning conservatively continues.
  * If either index is exhausted (or the scanned prefixes cover every
    object) all objects are already complete candidates and the ranking
    is final. A frontier that has not delivered its first item leaves T
    undefined -- the unknown bound is never filled with 0. Empty data
    simply exhausts both indexes on the first scan.
"""
from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request
from dataclasses import dataclass

COLUMNS = ('x', 'y')
MAX_LOOKUP_IDS = 4  # protocol: /lookup accepts at most 4 ids per request


class SnapshotChangedError(RuntimeError):
    """The server snapshot changed mid-query.

    The query fails and no result is published; scores gathered under the
    old snapshot are never combined with another snapshot, and there is no
    silent retry that would keep stale candidates.
    """


class QueryError(RuntimeError):
    """Protocol-level failure (bad reply, no progress, inconsistent scores)."""


@dataclass
class Frontier:
    """Scan frontier of one index: cursor, last delivered score, exhausted."""
    cursor: int = 0
    last_score: int | None = None
    exhausted: bool = False

    def state(self) -> dict:
        return {'cursor': self.cursor,
                'last_score': self.last_score,
                'exhausted': self.exhausted}


class IndexClient:
    """Snapshot-pinning HTTP wrapper for /info, /scan and /lookup."""

    def __init__(self, base_url: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        with urllib.request.urlopen(self.base_url + '/info', timeout=self.timeout) as resp:
            info = json.loads(resp.read())
        self.snapshot = info['snapshot']
        self.count = int(info['count'])
        self.columns = list(info['columns'])

    def _post(self, path: str, payload: dict) -> dict:
        body = dict(payload, snapshot=self.snapshot)
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                reply = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            try:
                reply = json.loads(e.read() or b'{}')
            except ValueError:
                reply = {}
            if e.code == 409 or reply.get('error') == 'snapshot_changed':
                raise SnapshotChangedError(
                    f'{path} rejected with 409: server is on '
                    f'{reply.get("snapshot")!r}, query pinned {self.snapshot!r}') from None
            raise QueryError(f'{path} failed: HTTP {e.code} {reply}') from None
        if reply.get('snapshot') != self.snapshot:
            raise SnapshotChangedError(
                f'{path} reply snapshot {reply.get("snapshot")!r} '
                f'!= pinned {self.snapshot!r}')
        return reply

    def scan(self, column: str, cursor: int, limit: int) -> dict:
        return self._post('/scan', {'column': column, 'cursor': cursor, 'limit': limit})

    def lookup(self, ids: list[str]) -> dict:
        return self._post('/lookup', {'ids': ids})


def run_topk(client: IndexClient, k: int, weights: dict, page_size: int) -> dict:
    """Run one top-K query. Raises SnapshotChangedError / QueryError on failure."""
    try:
        wx, wy = int(weights['x']), int(weights['y'])
    except (KeyError, TypeError, ValueError):
        raise QueryError(f'weights must be a dict with integer x/y: {weights!r}') from None
    if wx <= 0 or wy <= 0:
        raise QueryError('weights must be positive integers')
    if not isinstance(k, int) or k < 1:
        raise QueryError('k must be a positive integer')
    if not 1 <= page_size <= 3:
        raise QueryError('page_size must be within the server limit 1..3')

    fx, fy = Frontier(), Frontier()
    frontiers = {'x': fx, 'y': fy}
    complete: dict[str, tuple[int, int]] = {}  # id -> (x, y); seen implies complete
    request_log: list[dict] = []
    tie_notices: list[str] = []
    n_scan = n_lookup = scan_items = lookup_items = 0
    turn = 0

    def total_of(scores: tuple[int, int]) -> int:
        return wx * scores[0] + wy * scores[1]

    def ranked() -> list:
        # total desc, then id UTF-8 byte order asc (same key the server uses)
        return sorted(complete.items(),
                      key=lambda kv: (-total_of(kv[1]), kv[0].encode()))

    def unseen_bound() -> int | None:
        """Upper bound on the total of any object not seen so far.

        None means the bound is unknown (a frontier has not delivered its
        first item yet) or vacuous (an index is exhausted, so no unseen
        objects exist). The unknown bound is never filled with 0.
        """
        if fx.exhausted or fy.exhausted:
            return None
        if fx.last_score is None or fy.last_score is None:
            return None
        return wx * fx.last_score + wy * fy.last_score

    def stop_state(reason: str, basis: str, kth: dict | None) -> dict:
        return {
            'reason': reason,
            'basis': basis,
            'seen_ids': sorted(complete),
            'frontiers': {'x': fx.state(), 'y': fy.state()},
            'unseen_upper_bound': unseen_bound(),
            'kth': kth,
            'tie_continues': list(tie_notices),
        }

    def check_stop() -> dict | None:
        if fx.exhausted or fy.exhausted or len(complete) >= client.count:
            done = [c for c in COLUMNS if frontiers[c].exhausted]
            if done:
                reason = 'index_exhausted'
                basis = (f"index '{done[0]}' exhausted after "
                         f'{frontiers[done[0]].cursor} item(s); all {client.count} '
                         'objects have been seen and completed, ranking is final')
            else:
                reason = 'all_objects_seen'
                basis = (f'all {client.count} objects covered by the two scanned '
                         'prefixes; ranking is final')
            r = ranked()
            kth = ({'id': r[k - 1][0], 'total': total_of(r[k - 1][1])}
                   if len(r) >= k else None)
            return stop_state(reason, basis, kth)
        if len(complete) < k:
            return None  # not enough complete candidates yet
        bound = unseen_bound()
        if bound is None:
            return None  # threshold undefined: a frontier has no first item yet
        kth_id, kth_scores = ranked()[k - 1]
        kth_total = total_of(kth_scores)
        if kth_total > bound:
            basis = (f'candidate #{k} {kth_id!r} totals {kth_total} > unseen upper '
                     f'bound T={bound} (= {wx}*last_x[{fx.last_score}] + '
                     f'{wy}*last_y[{fy.last_score}]); no unseen object can enter '
                     f'the top {k}')
            return stop_state('threshold', basis, {'id': kth_id, 'total': kth_total})
        if kth_total == bound:
            tie_notices.append(
                f'candidate #{k} total {kth_total} == T {bound} at frontiers '
                f'x={fx.state()} y={fy.state()}: an unseen object could tie with '
                'a smaller id, scanning continues')
        return None

    stop = None
    while stop is None:
        stop = check_stop()
        if stop is not None:
            break
        column = COLUMNS[turn % 2]  # deterministic round-robin
        turn += 1
        frontier = frontiers[column]
        if frontier.exhausted:
            continue  # unreachable (check_stop fires first); guard anyway
        cursor_before = frontier.cursor
        reply = client.scan(column, cursor_before, page_size)
        n_scan += 1
        items = reply['items']
        scan_items += len(items)
        frontier.cursor = int(reply['next'])
        frontier.exhausted = bool(reply['exhausted'])
        if items:
            frontier.last_score = int(items[-1]['score'])
        elif not frontier.exhausted and frontier.cursor <= cursor_before:
            raise QueryError(f"scan on '{column}' made no progress; aborting")
        request_log.append({
            'op': 'scan', 'column': column, 'cursor': cursor_before,
            'limit': page_size, 'returned_ids': [it['id'] for it in items],
            'next': frontier.cursor, 'exhausted': frontier.exhausted})
        # complete newly seen objects via random access; already-cached ids
        # (seen in the other index) are neither re-looked-up nor re-candidates
        fresh = [it for it in items if it['id'] not in complete]
        for i in range(0, len(fresh), MAX_LOOKUP_IDS):
            batch = fresh[i:i + MAX_LOOKUP_IDS]
            ids = [it['id'] for it in batch]
            reply = client.lookup(ids)
            n_lookup += 1
            lookup_items += len(reply['items'])
            got = {o['id']: o for o in reply['items']}
            if set(got) != set(ids):
                raise QueryError(
                    f'lookup replied ids {sorted(got)} != requested {sorted(ids)}')
            request_log.append({'op': 'lookup', 'ids': ids,
                                'returned_ids': [o['id'] for o in reply['items']]})
            for it in batch:
                obj = got[it['id']]
                x_score, y_score = int(obj['x']), int(obj['y'])
                observed = x_score if column == 'x' else y_score
                if int(it['score']) != observed:
                    raise QueryError(
                        f"score mismatch for {it['id']!r}: scan said "
                        f"{column}={it['score']}, lookup said {observed}; refusing "
                        'to combine values from different snapshots')
                complete[it['id']] = (x_score, y_score)

    hits = [{'rank': i + 1, 'id': oid, 'x': sc[0], 'y': sc[1], 'total': total_of(sc)}
            for i, (oid, sc) in enumerate(ranked()[:k])]  # [:k] yields all if k > count
    return {
        'status': 'ok',
        'snapshot': client.snapshot,
        'query': {'k': k, 'weights': {'x': wx, 'y': wy}, 'page_size': page_size},
        'object_count': client.count,
        'hits': hits,
        'stop': stop,
        'requests': {
            'scan_requests': n_scan,
            'lookup_requests': n_lookup,
            'total_requests': n_scan + n_lookup,
            'scan_items_returned': scan_items,
            'lookup_items_returned': lookup_items,
            'items_returned': scan_items + lookup_items,
            'full_scan_items': 2 * client.count,
            'log': request_log,
        },
    }


def format_report(rep: dict) -> str:
    """Human-readable rendering of one query report."""
    lines = []
    q = rep.get('query', {})
    header = f"k={q.get('k')} weights={q.get('weights')} page_size={q.get('page_size')}"
    if rep.get('status') != 'ok':
        lines.append(f'query FAILED ({header}): {rep.get("reason")}')
        lines.append('no result published')
        return '\n'.join(lines)
    lines.append(f"query ok ({header}) snapshot={rep['snapshot']} "
                 f"objects={rep['object_count']}")
    lines.append('hits:')
    for h in rep['hits']:
        lines.append(f"  #{h['rank']:<2} {h['id']:<4} x={h['x']:<3} y={h['y']:<3} "
                     f"total={h['total']}")
    s = rep['stop']
    lines.append(f"stop: {s['reason']} -- {s['basis']}")
    lines.append(f"  seen ({len(s['seen_ids'])}): {' '.join(s['seen_ids']) or '(none)'}")
    for c in COLUMNS:
        f = s['frontiers'][c]
        lines.append(f"  frontier {c}: cursor={f['cursor']} "
                     f"last_score={f['last_score']} exhausted={f['exhausted']}")
    lines.append(f"  unseen upper bound T: {s['unseen_upper_bound']}")
    if s.get('kth'):
        lines.append(f"  kth candidate: {s['kth']['id']} total={s['kth']['total']}")
    for note in s.get('tie_continues', []):
        lines.append(f'  tie-continue: {note}')
    rq = rep['requests']
    lines.append(f"requests: {rq['scan_requests']} scan + {rq['lookup_requests']} "
                 f"lookup = {rq['total_requests']}; items returned "
                 f"{rq['items_returned']} ({rq['scan_items_returned']} scan + "
                 f"{rq['lookup_items_returned']} lookup); two full index scans "
                 f"would return {rq['full_scan_items']} items")
    lines.append('request log:')
    for entry in rq['log']:
        if entry['op'] == 'scan':
            lines.append(f"  scan   {entry['column']} cursor={entry['cursor']} "
                         f"limit={entry['limit']} -> {entry['returned_ids']} "
                         f"next={entry['next']} exhausted={entry['exhausted']}")
        else:
            lines.append(f"  lookup {entry['ids']}")
    return '\n'.join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Exact threshold top-K client')
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--k', type=int, required=True)
    ap.add_argument('--wx', type=int, required=True)
    ap.add_argument('--wy', type=int, required=True)
    ap.add_argument('--page-size', type=int, default=2)
    args = ap.parse_args(argv)
    client = IndexClient(args.base_url)
    try:
        rep = run_topk(client, args.k, {'x': args.wx, 'y': args.wy}, args.page_size)
    except (SnapshotChangedError, QueryError) as e:
        rep = {'status': 'failed', 'reason': str(e), 'hits': None,
               'query': {'k': args.k, 'weights': {'x': args.wx, 'y': args.wy},
                         'page_size': args.page_size}}
    print(format_report(rep))
    print('\n' + json.dumps(rep, indent=2, ensure_ascii=False))
    return 0 if rep['status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
