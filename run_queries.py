#!/usr/bin/env python3
"""Run every query in fixtures/queries.json against a running index server.

Each query pins its own snapshot via a fresh IndexClient. A snapshot change
fails that query (no result published); there is no silent retry that would
reuse candidates gathered under another snapshot -- a retry is simply a new
run of this script with fresh state.
"""
import argparse
import json
import sys

from query_client import (IndexClient, QueryError, SnapshotChangedError,
                          format_report, run_topk)


def run_queries(base_url: str, queries: list[dict]) -> list[dict]:
    reports = []
    for q in queries:
        client = IndexClient(base_url)  # pin a fresh snapshot per query
        try:
            rep = run_topk(client, q['k'], q['weights'], q['page_size'])
        except (SnapshotChangedError, QueryError) as e:
            rep = {'status': 'failed', 'reason': str(e), 'hits': None,
                   'query': {'k': q['k'], 'weights': q['weights'],
                             'page_size': q['page_size']}}
        rep['query_id'] = q.get('id')
        reports.append(rep)
    return reports


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='Run fixture queries and report')
    ap.add_argument('--base-url', required=True)
    ap.add_argument('--queries', default='fixtures/queries.json')
    ap.add_argument('--out', default=None, help='also write full JSON reports here')
    args = ap.parse_args(argv)

    with open(args.queries) as f:
        queries = json.load(f)
    reports = run_queries(args.base_url, queries)

    for q, rep in zip(queries, reports):
        print(f"=== query {q.get('id')} ===")
        print(format_report(rep))
        print()

    print('=== request count summary ===')
    print(f"{'query':<8} {'k':>3} {'ps':>3} {'scan':>5} {'lookup':>7} "
          f"{'items':>6} {'2N(full)':>9}  stop")
    for q, rep in zip(queries, reports):
        if rep['status'] != 'ok':
            print(f"{q.get('id'):<8} FAILED: {rep['reason']}")
            continue
        rq = rep['requests']
        print(f"{q.get('id'):<8} {rep['query']['k']:>3} "
              f"{rep['query']['page_size']:>3} {rq['scan_requests']:>5} "
              f"{rq['lookup_requests']:>7} {rq['items_returned']:>6} "
              f"{rq['full_scan_items']:>9}  {rep['stop']['reason']}")

    if args.out:
        with open(args.out, 'w') as f:
            json.dump(reports, f, indent=2, ensure_ascii=False)
        print(f'full reports written to {args.out}')
    return 0 if all(r['status'] == 'ok' for r in reports) else 1


if __name__ == '__main__':
    sys.exit(main())
