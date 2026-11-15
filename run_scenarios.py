#!/usr/bin/env python3
"""Runs the scenarios from fixtures/scenarios.json against the real router and
records evidence under records/latest/:

    log.json   structured record of every step and check
    log.md     human-readable version
    data/      the live data root: primary.db, replica.db, cluster.json,
               bookmarks.db (the persistent bookmark store, kept as evidence)

The scenarios share one data root and run in the order given by
fixtures/scenarios.json: lag_and_clients, then snapshot, then generation.
An extra observe_validation section records the receipt rejection rules.

Hook scenarios inject events through router.Hooks, which fire from inside the
genuine read_report control flow. No step bypasses read_report.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import replica_fixture as fixture
import router


class Run:
    def __init__(self, root: Path):
        self.root = root
        self.steps: list[dict] = []
        self.checks: list[dict] = []
        self.t0 = time.monotonic()

    def record(self, label: str, data) -> None:
        entry = {'t_ms': round((time.monotonic() - self.t0) * 1000, 1),
                 'step': label, 'data': data}
        self.steps.append(entry)
        print(f"[{entry['t_ms']:>9}ms] {label}")

    def check(self, name: str, ok: bool, detail: str = '') -> None:
        self.checks.append({'check': name, 'passed': bool(ok), 'detail': detail})
        print(f"  {'PASS' if ok else 'FAIL'}: {name}" + (f' -- {detail}' if detail else ''))

    def cli(self, *args: str):
        """Invoke the real router CLI in a separate process (bookmarks must come
        from the persistent store on every call)."""
        cmd = [sys.executable, str(HERE / 'router.py'), '--root', str(self.root), *args]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        try:
            out = json.loads(proc.stdout)
        except json.JSONDecodeError:
            out = {'unparseable_stdout': proc.stdout, 'stderr': proc.stderr}
        self.record('cli: router.py ' + ' '.join(args),
                    {'exit_code': proc.returncode, 'output': out})
        return proc.returncode, out


def balances(res: dict):
    accounts = res.get('accounts')
    if not isinstance(accounts, list):
        return None
    return {a['id']: a['balance'] for a in accounts}


def check_invariant(run: Run, label: str, res: dict) -> None:
    """The invariant from fixtures/read-report.json must hold on every ok result."""
    if res.get('status') != 'ok':
        run.check(f'{label}: invariant', False, f"status={res.get('status')}")
        return
    total = sum(a['balance'] for a in res['accounts'])
    seq = res['snapshot_meta']['applied_seq']
    max_seq = res['history']['max_seq']
    ok = total == 1000 and (max_seq is None if seq == 0 else max_seq == seq)
    run.check(f'{label}: invariant sum(balances)=1000 & max_seq~applied_seq', ok,
              f'sum={total} applied_seq={seq} max_seq={max_seq}')


# --------------------------------------------------------------------------
# scenario 1: lag_and_clients
# --------------------------------------------------------------------------

def scenario_lag_and_clients(run: Run) -> None:
    run.record('scenario', {'id': 'lag_and_clients', 'source': 'fixtures/scenarios.json'})
    fixture.initialize(run.root)
    run.record('init', {'initialized': True})

    r1 = fixture.commit(run.root, 25)
    run.record('commit amount=25', r1)
    r2 = fixture.commit(run.root, 7)
    run.record('commit amount=7', r2)
    run.check('two commits landed on primary (seq 1, 2), replica NOT synced',
              (r1['commit_seq'], r2['commit_seq']) == (1, 2), f'{r1} {r2}')

    # A observes the second receipt; B observes nothing.
    code, obs = run.cli('observe', '--client', 'A', '--receipt', json.dumps(r2))
    run.check('A observe commit2 accepted, bookmark=2',
              code == 0 and obs.get('bookmark') == 2, json.dumps(obs))

    _, bm_a = run.cli('bookmark', '--client', 'A')
    _, bm_b = run.cli('bookmark', '--client', 'B')
    run.check('bookmarks survive process restarts; clients isolated (A=2, B=0)',
              bm_a.get('bookmark') == 2 and bm_b.get('bookmark') == 0,
              f"A={bm_a.get('bookmark')} B={bm_b.get('bookmark')}")

    # Replica is still at applied_seq=0. A must wait out wait_ms and fall back.
    _, ra = run.cli('read', '--client', 'A', '--wait-ms', '100', '--fallback', 'primary')
    run.check('A: replica too stale -> waits >= wait_ms, retries, falls back to primary',
              ra.get('status') == 'ok' and ra.get('node') == 'primary'
              and ra['snapshot_meta']['applied_seq'] == 2
              and ra['attempts'] >= 2 and ra['elapsed_ms'] >= 100,
              f"node={ra.get('node')} attempts={ra.get('attempts')} "
              f"elapsed_ms={ra.get('elapsed_ms')}")
    run.check('A: sees its own confirmed writes (a=568, b=432, max_seq=2)',
              balances(ra) == {'a': 568, 'b': 432}
              and ra.get('history') == {'max_seq': 2, 'n': 2},
              json.dumps({'accounts': ra.get('accounts'), 'history': ra.get('history')}))
    check_invariant(run, 'A primary read', ra)

    # B never observed anything: required_seq=0, the stale replica is fine.
    _, rb = run.cli('read', '--client', 'B', '--wait-ms', '100', '--fallback', 'none')
    run.check('B: no receipt -> required_seq=0 -> reads the stale replica immediately',
              rb.get('status') == 'ok' and rb.get('node') == 'replica'
              and rb['snapshot_meta']['applied_seq'] == 0 and rb['attempts'] == 1,
              f"node={rb.get('node')} attempts={rb.get('attempts')} "
              f"required_seq={rb.get('required_seq')}")
    run.check('B: reads pre-commit state (a=600, b=400, empty history)',
              balances(rb) == {'a': 600, 'b': 400}
              and rb.get('history') == {'max_seq': None, 'n': 0},
              json.dumps({'accounts': rb.get('accounts'), 'history': rb.get('history')}))
    check_invariant(run, 'B replica read', rb)

    _, bm_b2 = run.cli('bookmark', '--client', 'B')
    run.check('no cross-client pollution: B bookmark still 0 after all of A\'s activity',
              bm_b2.get('bookmark') == 0, json.dumps(bm_b2))

    rep = fixture.replicate(run.root)
    run.record('replicate', rep)

    _, ra2 = run.cli('read', '--client', 'A', '--wait-ms', '100', '--fallback', 'none')
    run.check('A: back to the replica after sync, no fallback needed',
              ra2.get('status') == 'ok' and ra2.get('node') == 'replica'
              and ra2['snapshot_meta']['applied_seq'] == 2 and ra2['attempts'] == 1,
              f"node={ra2.get('node')} attempts={ra2.get('attempts')}")
    run.check('A: replica now serves the post-commit state',
              balances(ra2) == {'a': 568, 'b': 432}
              and ra2.get('history') == {'max_seq': 2, 'n': 2},
              json.dumps({'accounts': ra2.get('accounts'), 'history': ra2.get('history')}))
    check_invariant(run, 'A replica read', ra2)


# --------------------------------------------------------------------------
# extra evidence: observe validation rules
# --------------------------------------------------------------------------

def scenario_observe_validation(run: Run) -> None:
    run.record('scenario', {'id': 'observe_validation',
                            'note': 'extra evidence for receipt rejection rules '
                                    '(A currently holds bookmark=2)'})
    old = {'cluster_id': 'demo-cluster', 'generation': 1, 'commit_seq': 1}
    code, out = run.cli('observe', '--client', 'A', '--receipt', json.dumps(old))
    run.check('out-of-order/duplicate receipt does not regress the bookmark',
              code == 0 and out.get('bookmark') == 2 and out.get('advanced') is False,
              json.dumps(out))

    bad = {'cluster_id': 'other-cluster', 'generation': 1, 'commit_seq': 5}
    code, out = run.cli('observe', '--client', 'A', '--receipt', json.dumps(bad))
    run.check('wrong cluster -> explicit cluster_mismatch error (exit 2)',
              code == 2 and out.get('error') == 'cluster_mismatch', json.dumps(out))

    bad = {'cluster_id': 'demo-cluster', 'generation': 2, 'commit_seq': 5}
    code, out = run.cli('observe', '--client', 'A', '--receipt', json.dumps(bad))
    run.check('wrong generation -> explicit generation_mismatch error (exit 2)',
              code == 2 and out.get('error') == 'generation_mismatch', json.dumps(out))

    bad = {'cluster_id': 'demo-cluster', 'generation': 1, 'commit_seq': -1}
    code, out = run.cli('observe', '--client', 'A', '--receipt', json.dumps(bad))
    run.check('negative seq -> explicit invalid_seq error (exit 2)',
              code == 2 and out.get('error') == 'invalid_seq', json.dumps(out))

    _, bm = run.cli('bookmark', '--client', 'A')
    run.check('rejections never clear or move the stored bookmark (still 2)',
              bm.get('bookmark') == 2, json.dumps(bm))


# --------------------------------------------------------------------------
# scenario 2: snapshot
# --------------------------------------------------------------------------

def scenario_snapshot(run: Run) -> None:
    run.record('scenario', {'id': 'snapshot', 'source': 'fixtures/scenarios.json'})
    rep = fixture.replicate(run.root)
    run.record('replicate', rep)

    hook_events = []

    def after_accounts():
        # Fires inside A's open read transaction, between the accounts query
        # and the history query. The commit + replicate really land on disk
        # while the reader's snapshot is pinned.
        r = fixture.commit(run.root, 3)
        rp = fixture.replicate(run.root)
        hook_events.append({'commit': r, 'replicate': rp})
        run.record('hook after_accounts: commit amount=3 + replicate '
                   '(mid-transaction, same reader snapshot)', {'commit': r, 'replicate': rp})

    hooks = router.Hooks(after_accounts=after_accounts)
    res = router.read_report(run.root, 'A', 100, 'none', hooks=hooks)
    run.record('A read_report (after_accounts hook injects commit+replicate)', res)

    run.check('read succeeded on the replica in a single attempt',
              res['status'] == 'ok' and res['node'] == 'replica' and res['attempts'] == 1,
              json.dumps(res.get('snapshot_meta')))
    run.check('hook fired exactly once, between the two report queries',
              len(hook_events) == 1, json.dumps(hook_events))
    run.check('report not mixed: meta/accounts/history all from the seq=2 snapshot '
              'even though the replica reached seq=3 mid-transaction',
              res['snapshot_meta']['applied_seq'] == 2
              and balances(res) == {'a': 568, 'b': 432}
              and res['history'] == {'max_seq': 2, 'n': 2},
              json.dumps({'meta': res['snapshot_meta'], 'accounts': res.get('accounts'),
                          'history': res.get('history')}))
    check_invariant(run, 'snapshot read', res)

    # Independent proof that the replica really advanced underneath the reader.
    meta = sqlite3.connect(run.root / 'replica.db').execute(
        'SELECT cluster_id, generation, applied_seq FROM meta').fetchone()
    run.record('independent replica meta after the read finished',
               dict(zip(('cluster_id', 'generation', 'applied_seq'), meta)))
    run.check('replica really is at seq=3 now (the race was real, not staged)',
              meta[2] == 3, f'applied_seq={meta[2]}')


# --------------------------------------------------------------------------
# scenario 3: generation
# --------------------------------------------------------------------------

def scenario_generation(run: Run) -> None:
    run.record('scenario', {'id': 'generation', 'source': 'fixtures/scenarios.json'})
    hook_events = []

    def after_node_selected(node: str):
        # Fires after the replica path is chosen but before BEGIN.
        fixture.reset(run.root, node, 2)  # replica generation 1 -> 2
        hook_events.append(node)
        run.record(f'hook after_node_selected({node}): reset-node generation=2',
                   {'node': node, 'generation': 2})

    hooks = router.Hooks(after_node_selected=after_node_selected)
    # fallback=primary is available on purpose: identity_mismatch must NOT
    # silently fall back to it.
    res = router.read_report(run.root, 'A', 100, 'primary', hooks=hooks)
    run.record('A read_report (after_node_selected hook resets generation; '
               'fallback=primary available)', res)

    run.check('identity validated in the real snapshot -> explicit identity_mismatch',
              res['status'] == 'identity_mismatch' and res['node'] == 'replica'
              and res['snapshot_meta']['generation'] == 2
              and res['expected_identity']['generation'] == 1,
              json.dumps({'snapshot_meta': res.get('snapshot_meta'),
                          'expected': res.get('expected_identity')}))
    run.check('not treated as lag: no retry storm, no implicit fallback to primary',
              res['attempts'] == 1 and res['elapsed_ms'] < 100,
              f"attempts={res['attempts']} elapsed_ms={res['elapsed_ms']}")
    run.check('hook fired exactly once, before BEGIN', hook_events == ['replica'],
              json.dumps(hook_events))


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def render_md(log: dict) -> str:
    lines = ['# Scenario run record', '',
             f"checks: {log['summary']['passed']} passed, "
             f"{log['summary']['failed']} failed", '',
             '## Checks', '']
    for c in log['checks']:
        mark = 'x' if c['passed'] else ' '
        lines.append(f"- [{mark}] {c['check']}" + (f" -- {c['detail']}" if c['detail'] else ''))
    lines += ['', '## Steps', '']
    for s in log['steps']:
        lines.append(f"### [{s['t_ms']}ms] {s['step']}")
        lines.append('')
        lines.append('```json')
        lines.append(json.dumps(s['data'], indent=2, ensure_ascii=False))
        lines.append('```')
        lines.append('')
    return '\n'.join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--records', type=Path, default=HERE / 'records' / 'latest',
                    help='where to write log.json, log.md and the data root '
                         '(recreated on every run)')
    args = ap.parse_args()
    records = args.records
    root = records / 'data'
    if root.exists():
        shutil.rmtree(root)  # dedicated scratch dir owned by this runner
    records.mkdir(parents=True, exist_ok=True)

    run = Run(root)
    scenario_lag_and_clients(run)
    scenario_observe_validation(run)
    scenario_snapshot(run)
    scenario_generation(run)

    passed = sum(1 for c in run.checks if c['passed'])
    log = {'summary': {'passed': passed, 'failed': len(run.checks) - passed,
                       'scenarios': ['lag_and_clients', 'observe_validation',
                                     'snapshot', 'generation'],
                       'data_root': str(root)},
           'checks': run.checks, 'steps': run.steps}
    (records / 'log.json').write_text(json.dumps(log, indent=2, ensure_ascii=False))
    (records / 'log.md').write_text(render_md(log))
    print(f'\n{passed}/{len(run.checks)} checks passed; records written to {records}')
    return 0 if passed == len(run.checks) else 1


if __name__ == '__main__':
    sys.exit(main())
