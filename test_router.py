#!/usr/bin/env python3
"""Unit tests for router.py (bookmarks, observe validation, read routing,
snapshot consistency, identity handling). Run: python3 test_router.py -v"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import replica_fixture as fixture
import router
from router import RouterError

CLUSTER = {'cluster_id': 'demo-cluster', 'generation': 1}


def receipt(seq, **over):
    r = {'cluster_id': 'demo-cluster', 'generation': 1, 'commit_seq': seq}
    r.update(over)
    return r


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fixture.initialize(self.root)

    def tearDown(self):
        self.tmp.cleanup()


class ObserveTests(Base):
    def test_accept_and_max_never_regresses(self):
        out = router.observe(self.root, 'A', receipt(2))
        self.assertEqual((out['previous_bookmark'], out['bookmark'], out['advanced']),
                         (0, 2, True))
        # duplicate
        out = router.observe(self.root, 'A', receipt(2))
        self.assertEqual((out['bookmark'], out['advanced']), (2, False))
        # out-of-order (lower)
        out = router.observe(self.root, 'A', receipt(1))
        self.assertEqual((out['bookmark'], out['advanced']), (2, False))
        # zero is a valid seq
        out = router.observe(self.root, 'A', receipt(0))
        self.assertEqual(out['bookmark'], 2)

    def test_clients_isolated(self):
        router.observe(self.root, 'A', receipt(5))
        self.assertEqual(router.get_bookmark(self.root, 'A'), 5)
        self.assertEqual(router.get_bookmark(self.root, 'B'), 0)
        router.observe(self.root, 'B', receipt(1))
        self.assertEqual(router.get_bookmark(self.root, 'A'), 5)
        self.assertEqual(router.get_bookmark(self.root, 'B'), 1)

    def test_persistence_on_disk(self):
        router.observe(self.root, 'A', receipt(3))
        self.assertTrue((self.root / 'bookmarks.db').exists())
        # fresh connection, as after a process restart
        self.assertEqual(router.get_bookmark(self.root, 'A'), 3)

    def test_wrong_cluster_errors_and_keeps_bookmark(self):
        router.observe(self.root, 'A', receipt(2))
        with self.assertRaises(RouterError) as cm:
            router.observe(self.root, 'A', receipt(9, cluster_id='other'))
        self.assertEqual(cm.exception.err_type, 'cluster_mismatch')
        self.assertEqual(router.get_bookmark(self.root, 'A'), 2)

    def test_wrong_generation_errors_and_keeps_bookmark(self):
        router.observe(self.root, 'A', receipt(2))
        with self.assertRaises(RouterError) as cm:
            router.observe(self.root, 'A', receipt(9, generation=2))
        self.assertEqual(cm.exception.err_type, 'generation_mismatch')
        self.assertEqual(router.get_bookmark(self.root, 'A'), 2)

    def test_bad_seq_rejected(self):
        for bad in (-1, 1.5, '1', True, None, [1]):
            with self.assertRaises(RouterError) as cm:
                router.observe(self.root, 'A', receipt(bad))
            self.assertEqual(cm.exception.err_type, 'invalid_seq', f'seq={bad!r}')
        self.assertEqual(router.get_bookmark(self.root, 'A'), 0)

    def test_bad_receipt_shape_rejected(self):
        for bad in (None, [], 'x', {}, {'cluster_id': 'demo-cluster'},
                    {'cluster_id': 'demo-cluster', 'generation': 1}):
            with self.assertRaises(RouterError):
                router.observe(self.root, 'A', bad)

    def test_missing_cluster_json(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RouterError) as cm:
                router.observe(Path(d), 'A', receipt(1))
            self.assertEqual(cm.exception.err_type, 'not_initialized')

    def test_bad_client_id(self):
        for bad in ('', '  ', None, 5, 'x' * 200):
            with self.assertRaises(RouterError):
                router.observe(self.root, bad, receipt(1))


class ReadTests(Base):
    def test_fresh_client_reads_stale_replica(self):
        out = router.read_report(self.root, 'B', 0, 'none')
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(out['node'], 'replica')
        self.assertEqual(out['required_seq'], 0)
        self.assertEqual(out['snapshot_meta'],
                         {'cluster_id': 'demo-cluster', 'generation': 1, 'applied_seq': 0})
        self.assertEqual(out['history'], {'max_seq': None, 'n': 0})
        self.assertEqual(sum(a['balance'] for a in out['accounts']), 1000)
        self.assertEqual(out['attempts'], 1)

    def test_lag_not_ready_returns_no_stale_data(self):
        r = fixture.commit(self.root, 25)
        router.observe(self.root, 'A', r)
        t0 = time.monotonic()
        out = router.read_report(self.root, 'A', 150, 'none')
        elapsed = time.monotonic() - t0
        self.assertEqual(out['status'], 'not_ready')
        self.assertIsNone(out['node'])
        self.assertNotIn('accounts', out)
        self.assertNotIn('history', out)
        self.assertEqual(out['observed_meta']['replica']['applied_seq'], 0)
        self.assertGreaterEqual(elapsed, 0.15)  # really waited the full budget
        self.assertGreaterEqual(out['attempts'], 2)  # really retried

    def test_fallback_primary_then_back_to_replica(self):
        fixture.commit(self.root, 25)
        r2 = fixture.commit(self.root, 7)
        router.observe(self.root, 'A', r2)
        out = router.read_report(self.root, 'A', 100, 'primary')
        self.assertEqual((out['status'], out['node']), ('ok', 'primary'))
        self.assertEqual(out['snapshot_meta']['applied_seq'], 2)
        self.assertEqual(out['history'], {'max_seq': 2, 'n': 2})
        self.assertEqual({a['id']: a['balance'] for a in out['accounts']},
                         {'a': 568, 'b': 432})
        fixture.replicate(self.root)
        out = router.read_report(self.root, 'A', 100, 'none')
        self.assertEqual((out['status'], out['node']), ('ok', 'replica'))
        self.assertEqual(out['snapshot_meta']['applied_seq'], 2)

    def test_identity_mismatch_on_replica_is_not_lag(self):
        fixture.commit(self.root, 5)
        fixture.reset(self.root, 'replica', 2)
        t0 = time.monotonic()
        out = router.read_report(self.root, 'A', 500, 'primary')
        elapsed = time.monotonic() - t0
        self.assertEqual(out['status'], 'identity_mismatch')
        self.assertEqual(out['node'], 'replica')
        self.assertEqual(out['snapshot_meta']['generation'], 2)
        self.assertEqual(out['attempts'], 1)       # no retries
        self.assertLess(elapsed, 0.5)              # did not wait out the budget
        self.assertNotIn('accounts', out)          # no implicit fallback data

    def test_identity_mismatch_on_primary_fallback(self):
        r = fixture.commit(self.root, 5)
        router.observe(self.root, 'A', r)  # required=1, replica stays at 0
        fixture.reset(self.root, 'primary', 9)
        out = router.read_report(self.root, 'A', 50, 'primary')
        self.assertEqual(out['status'], 'identity_mismatch')
        self.assertEqual(out['node'], 'primary')
        self.assertEqual(out['snapshot_meta']['generation'], 9)

    def test_primary_also_behind_is_not_ready(self):
        r = fixture.commit(self.root, 5)
        router.observe(self.root, 'A', r)          # required=1
        fixture.reset(self.root, 'primary', 1)     # same generation, applied_seq=0
        out = router.read_report(self.root, 'A', 30, 'primary')
        self.assertEqual(out['status'], 'not_ready')
        self.assertEqual(out['observed_meta']['primary']['applied_seq'], 0)

    def test_snapshot_consistency_with_mid_transaction_writes(self):
        r = fixture.commit(self.root, 25)
        fixture.replicate(self.root)
        router.observe(self.root, 'A', r)          # required=1, replica at 1
        fired = []

        def hook():
            fixture.commit(self.root, 3)           # primary -> seq 2
            fixture.replicate(self.root)           # replica -> seq 2
            fired.append(True)

        hooks = router.Hooks(after_accounts=hook)
        out = router.read_report(self.root, 'A', 100, 'none', hooks=hooks)
        self.assertEqual(out['status'], 'ok')
        self.assertEqual(fired, [True])
        # everything from the seq=1 snapshot despite the mid-read sync
        self.assertEqual(out['snapshot_meta']['applied_seq'], 1)
        self.assertEqual(out['history'], {'max_seq': 1, 'n': 1})
        self.assertEqual({a['id']: a['balance'] for a in out['accounts']},
                         {'a': 575, 'b': 425})
        # and the replica really did move on underneath the reader
        meta = sqlite3.connect(self.root / 'replica.db').execute(
            'SELECT applied_seq FROM meta').fetchone()
        self.assertEqual(meta[0], 2)

    def test_generation_reset_between_node_choice_and_begin(self):
        def hook(node):
            fixture.reset(self.root, node, 2)

        hooks = router.Hooks(after_node_selected=hook)
        out = router.read_report(self.root, 'A', 100, 'primary', hooks=hooks)
        self.assertEqual(out['status'], 'identity_mismatch')
        self.assertEqual(out['node'], 'replica')
        self.assertEqual(out['attempts'], 1)

    def test_bad_arguments(self):
        with self.assertRaises(RouterError):
            router.read_report(self.root, 'A', -1, 'none')
        with self.assertRaises(RouterError):
            router.read_report(self.root, 'A', 0, 'sometimes')
        with self.assertRaises(RouterError):
            router.read_report(self.root, 'A', True, 'none')

    def test_read_does_not_touch_bookmarks(self):
        router.observe(self.root, 'A', receipt(4))
        router.read_report(self.root, 'A', 0, 'none')
        self.assertEqual(router.get_bookmark(self.root, 'A'), 4)
        self.assertEqual(router.get_bookmark(self.root, 'B'), 0)


if __name__ == '__main__':
    unittest.main(verbosity=2)
