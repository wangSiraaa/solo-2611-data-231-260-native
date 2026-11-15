# Scenario run record

checks: 26 passed, 0 failed

## Checks

- [x] two commits landed on primary (seq 1, 2), replica NOT synced -- {'cluster_id': 'demo-cluster', 'generation': 1, 'commit_seq': 1} {'cluster_id': 'demo-cluster', 'generation': 1, 'commit_seq': 2}
- [x] A observe commit2 accepted, bookmark=2 -- {"status": "ok", "client_id": "A", "receipt_seq": 2, "previous_bookmark": 0, "bookmark": 2, "advanced": true}
- [x] bookmarks survive process restarts; clients isolated (A=2, B=0) -- A=2 B=0
- [x] A: replica too stale -> waits >= wait_ms, retries, falls back to primary -- node=primary attempts=7 elapsed_ms=104.125
- [x] A: sees its own confirmed writes (a=568, b=432, max_seq=2) -- {"accounts": [{"id": "a", "balance": 568}, {"id": "b", "balance": 432}], "history": {"max_seq": 2, "n": 2}}
- [x] A primary read: invariant sum(balances)=1000 & max_seq~applied_seq -- sum=1000 applied_seq=2 max_seq=2
- [x] B: no receipt -> required_seq=0 -> reads the stale replica immediately -- node=replica attempts=1 required_seq=0
- [x] B: reads pre-commit state (a=600, b=400, empty history) -- {"accounts": [{"id": "a", "balance": 600}, {"id": "b", "balance": 400}], "history": {"max_seq": null, "n": 0}}
- [x] B replica read: invariant sum(balances)=1000 & max_seq~applied_seq -- sum=1000 applied_seq=0 max_seq=None
- [x] no cross-client pollution: B bookmark still 0 after all of A's activity -- {"status": "ok", "client_id": "B", "bookmark": 0}
- [x] A: back to the replica after sync, no fallback needed -- node=replica attempts=1
- [x] A: replica now serves the post-commit state -- {"accounts": [{"id": "a", "balance": 568}, {"id": "b", "balance": 432}], "history": {"max_seq": 2, "n": 2}}
- [x] A replica read: invariant sum(balances)=1000 & max_seq~applied_seq -- sum=1000 applied_seq=2 max_seq=2
- [x] out-of-order/duplicate receipt does not regress the bookmark -- {"status": "ok", "client_id": "A", "receipt_seq": 1, "previous_bookmark": 2, "bookmark": 2, "advanced": false}
- [x] wrong cluster -> explicit cluster_mismatch error (exit 2) -- {"status": "error", "error": "cluster_mismatch", "detail": "receipt cluster_id 'other-cluster' != expected 'demo-cluster'; bookmark left untouched"}
- [x] wrong generation -> explicit generation_mismatch error (exit 2) -- {"status": "error", "error": "generation_mismatch", "detail": "receipt generation 2 != expected 1; bookmark left untouched"}
- [x] negative seq -> explicit invalid_seq error (exit 2) -- {"status": "error", "error": "invalid_seq", "detail": "commit_seq must be a non-negative integer, got -1"}
- [x] rejections never clear or move the stored bookmark (still 2) -- {"status": "ok", "client_id": "A", "bookmark": 2}
- [x] read succeeded on the replica in a single attempt -- {"cluster_id": "demo-cluster", "generation": 1, "applied_seq": 2}
- [x] hook fired exactly once, between the two report queries -- [{"commit": {"cluster_id": "demo-cluster", "generation": 1, "commit_seq": 3}, "replicate": {"cluster_id": "demo-cluster", "generation": 1, "commit_seq": 3}}]
- [x] report not mixed: meta/accounts/history all from the seq=2 snapshot even though the replica reached seq=3 mid-transaction -- {"meta": {"cluster_id": "demo-cluster", "generation": 1, "applied_seq": 2}, "accounts": [{"id": "a", "balance": 568}, {"id": "b", "balance": 432}], "history": {"max_seq": 2, "n": 2}}
- [x] snapshot read: invariant sum(balances)=1000 & max_seq~applied_seq -- sum=1000 applied_seq=2 max_seq=2
- [x] replica really is at seq=3 now (the race was real, not staged) -- applied_seq=3
- [x] identity validated in the real snapshot -> explicit identity_mismatch -- {"snapshot_meta": {"cluster_id": "demo-cluster", "generation": 2, "applied_seq": 0}, "expected": {"cluster_id": "demo-cluster", "generation": 1}}
- [x] not treated as lag: no retry storm, no implicit fallback to primary -- attempts=1 elapsed_ms=1.175
- [x] hook fired exactly once, before BEGIN -- ["replica"]

## Steps

### [0.0ms] scenario

```json
{
  "id": "lag_and_clients",
  "source": "fixtures/scenarios.json"
}
```

### [11.5ms] init

```json
{
  "initialized": true
}
```

### [14.2ms] commit amount=25

```json
{
  "cluster_id": "demo-cluster",
  "generation": 1,
  "commit_seq": 1
}
```

### [17.1ms] commit amount=7

```json
{
  "cluster_id": "demo-cluster",
  "generation": 1,
  "commit_seq": 2
}
```

### [43.8ms] cli: router.py observe --client A --receipt {"cluster_id": "demo-cluster", "generation": 1, "commit_seq": 2}

```json
{
  "exit_code": 0,
  "output": {
    "status": "ok",
    "client_id": "A",
    "receipt_seq": 2,
    "previous_bookmark": 0,
    "bookmark": 2,
    "advanced": true
  }
}
```

### [65.1ms] cli: router.py bookmark --client A

```json
{
  "exit_code": 0,
  "output": {
    "status": "ok",
    "client_id": "A",
    "bookmark": 2
  }
}
```

### [86.1ms] cli: router.py bookmark --client B

```json
{
  "exit_code": 0,
  "output": {
    "status": "ok",
    "client_id": "B",
    "bookmark": 0
  }
}
```

### [211.8ms] cli: router.py read --client A --wait-ms 100 --fallback primary

```json
{
  "exit_code": 0,
  "output": {
    "client_id": "A",
    "required_seq": 2,
    "expected_identity": {
      "cluster_id": "demo-cluster",
      "generation": 1
    },
    "wait_ms": 100,
    "fallback": "primary",
    "attempts": 7,
    "elapsed_ms": 104.125,
    "status": "ok",
    "node": "primary",
    "snapshot_meta": {
      "cluster_id": "demo-cluster",
      "generation": 1,
      "applied_seq": 2
    },
    "accounts": [
      {
        "id": "a",
        "balance": 568
      },
      {
        "id": "b",
        "balance": 432
      }
    ],
    "history": {
      "max_seq": 2,
      "n": 2
    }
  }
}
```

### [233.7ms] cli: router.py read --client B --wait-ms 100 --fallback none

```json
{
  "exit_code": 0,
  "output": {
    "client_id": "B",
    "required_seq": 0,
    "expected_identity": {
      "cluster_id": "demo-cluster",
      "generation": 1
    },
    "wait_ms": 100,
    "fallback": "none",
    "attempts": 1,
    "elapsed_ms": 1.431,
    "status": "ok",
    "node": "replica",
    "snapshot_meta": {
      "cluster_id": "demo-cluster",
      "generation": 1,
      "applied_seq": 0
    },
    "accounts": [
      {
        "id": "a",
        "balance": 600
      },
      {
        "id": "b",
        "balance": 400
      }
    ],
    "history": {
      "max_seq": null,
      "n": 0
    }
  }
}
```

### [253.1ms] cli: router.py bookmark --client B

```json
{
  "exit_code": 0,
  "output": {
    "status": "ok",
    "client_id": "B",
    "bookmark": 0
  }
}
```

### [257.4ms] replicate

```json
{
  "cluster_id": "demo-cluster",
  "generation": 1,
  "commit_seq": 2
}
```

### [277.8ms] cli: router.py read --client A --wait-ms 100 --fallback none

```json
{
  "exit_code": 0,
  "output": {
    "client_id": "A",
    "required_seq": 2,
    "expected_identity": {
      "cluster_id": "demo-cluster",
      "generation": 1
    },
    "wait_ms": 100,
    "fallback": "none",
    "attempts": 1,
    "elapsed_ms": 1.433,
    "status": "ok",
    "node": "replica",
    "snapshot_meta": {
      "cluster_id": "demo-cluster",
      "generation": 1,
      "applied_seq": 2
    },
    "accounts": [
      {
        "id": "a",
        "balance": 568
      },
      {
        "id": "b",
        "balance": 432
      }
    ],
    "history": {
      "max_seq": 2,
      "n": 2
    }
  }
}
```

### [277.8ms] scenario

```json
{
  "id": "observe_validation",
  "note": "extra evidence for receipt rejection rules (A currently holds bookmark=2)"
}
```

### [296.2ms] cli: router.py observe --client A --receipt {"cluster_id": "demo-cluster", "generation": 1, "commit_seq": 1}

```json
{
  "exit_code": 0,
  "output": {
    "status": "ok",
    "client_id": "A",
    "receipt_seq": 1,
    "previous_bookmark": 2,
    "bookmark": 2,
    "advanced": false
  }
}
```

### [314.0ms] cli: router.py observe --client A --receipt {"cluster_id": "other-cluster", "generation": 1, "commit_seq": 5}

```json
{
  "exit_code": 2,
  "output": {
    "status": "error",
    "error": "cluster_mismatch",
    "detail": "receipt cluster_id 'other-cluster' != expected 'demo-cluster'; bookmark left untouched"
  }
}
```

### [332.9ms] cli: router.py observe --client A --receipt {"cluster_id": "demo-cluster", "generation": 2, "commit_seq": 5}

```json
{
  "exit_code": 2,
  "output": {
    "status": "error",
    "error": "generation_mismatch",
    "detail": "receipt generation 2 != expected 1; bookmark left untouched"
  }
}
```

### [351.2ms] cli: router.py observe --client A --receipt {"cluster_id": "demo-cluster", "generation": 1, "commit_seq": -1}

```json
{
  "exit_code": 2,
  "output": {
    "status": "error",
    "error": "invalid_seq",
    "detail": "commit_seq must be a non-negative integer, got -1"
  }
}
```

### [369.4ms] cli: router.py bookmark --client A

```json
{
  "exit_code": 0,
  "output": {
    "status": "ok",
    "client_id": "A",
    "bookmark": 2
  }
}
```

### [369.4ms] scenario

```json
{
  "id": "snapshot",
  "source": "fixtures/scenarios.json"
}
```

### [372.5ms] replicate

```json
{
  "cluster_id": "demo-cluster",
  "generation": 1,
  "commit_seq": 2
}
```

### [378.1ms] hook after_accounts: commit amount=3 + replicate (mid-transaction, same reader snapshot)

```json
{
  "commit": {
    "cluster_id": "demo-cluster",
    "generation": 1,
    "commit_seq": 3
  },
  "replicate": {
    "cluster_id": "demo-cluster",
    "generation": 1,
    "commit_seq": 3
  }
}
```

### [379.0ms] A read_report (after_accounts hook injects commit+replicate)

```json
{
  "client_id": "A",
  "required_seq": 2,
  "expected_identity": {
    "cluster_id": "demo-cluster",
    "generation": 1
  },
  "wait_ms": 100,
  "fallback": "none",
  "attempts": 1,
  "elapsed_ms": 5.906,
  "status": "ok",
  "node": "replica",
  "snapshot_meta": {
    "cluster_id": "demo-cluster",
    "generation": 1,
    "applied_seq": 2
  },
  "accounts": [
    {
      "id": "a",
      "balance": 568
    },
    {
      "id": "b",
      "balance": 432
    }
  ],
  "history": {
    "max_seq": 2,
    "n": 2
  }
}
```

### [379.9ms] independent replica meta after the read finished

```json
{
  "cluster_id": "demo-cluster",
  "generation": 1,
  "applied_seq": 3
}
```

### [379.9ms] scenario

```json
{
  "id": "generation",
  "source": "fixtures/scenarios.json"
}
```

### [381.5ms] hook after_node_selected(replica): reset-node generation=2

```json
{
  "node": "replica",
  "generation": 2
}
```

### [381.7ms] A read_report (after_node_selected hook resets generation; fallback=primary available)

```json
{
  "client_id": "A",
  "required_seq": 2,
  "expected_identity": {
    "cluster_id": "demo-cluster",
    "generation": 1
  },
  "wait_ms": 100,
  "fallback": "primary",
  "attempts": 1,
  "elapsed_ms": 1.175,
  "status": "identity_mismatch",
  "node": "replica",
  "snapshot_meta": {
    "cluster_id": "demo-cluster",
    "generation": 2,
    "applied_seq": 0
  },
  "error": "node 'replica' identity 'demo-cluster'/generation 2 != expected 'demo-cluster'/generation 1; explicit identity_mismatch, no implicit fallback"
}
```
