# Document Change Ingester

A small, offline Python 3.11 program that keeps a set of JSON documents under
RFC 6902 (JSON Patch) revision. Each input line is one **atomic** batch: either
every operation in the batch is applied, or the document is left exactly as it
was before the batch. A rejected batch never stops the run — the next batch is
processed against the unchanged document.

* Reads `fixtures/initial.json` — `{"docs": {doc_id: <JSON value>}}`.
* Reads `fixtures/patches.jsonl` — one `{"batch_id", "doc_id", "ops"}` per line.
* Writes `documents.json` (final document set) and `batches.jsonl` (one result
  record per batch).

The patch engine is implemented in this repository (pointers, path resolution,
operation semantics, atomic batches). Only `json`/`hashlib` from the standard
library are used at runtime; no third-party JSON Patch executor is called by the
production path. The test suite *optionally* compares results against the
`jsonpatch` package when that package is installed.

## Quick start

```bash
python3 -m docchange fixtures/initial.json fixtures/patches.jsonl -o output
```

This prints `processed 8 batch(es): 5 accepted, 3 rejected; 2 document(s) written`
to stderr and creates:

| file | contents |
| --- | --- |
| `output/documents.json` | `{"docs": {...}}` — final store |
| `output/batches.jsonl` | one JSON object per input batch |

## Library API

```python
from docchange import apply_batch

documents = {"d1": {"items": [1, 2, 3]}}
result = apply_batch(documents, {
    "batch_id": "b1",
    "doc_id": "d1",
    "ops": [
        {"op": "move", "from": "/items/0", "path": "/items/2"},
        {"op": "test", "path": "/items/2", "value": 1},
    ],
})
# result["status"] == "accepted"; documents["d1"]["items"] == [2, 3, 1]
```

`apply_batch(documents, batch)` mutates `documents` **only** when the batch is
accepted. It returns a JSON-serializable record:

```json
{
  "batch_id": "p3", "doc_id": "d1", "status": "rejected",
  "op_index": 1, "op": "test", "path": "/optional", "from": null,
  "reason": "test_failed",
  "message": "test failed: document value does not structurally match ...",
  "summary_before": {"sha256": "…", "bytes": 129, "type": "object", "size": 3},
  "summary_after":  {"sha256": "…", "bytes": 129, "type": "object", "size": 3},
  "line": 3
}
```

`line` is the 1-based input line of the batch. Fields that do not apply
(no failing op, no `from`, unknown doc, malformed line) are `null`; unknown
documents and unparseable lines have no document to summarize, so their
summaries are `null` too.

`summary_before`/`summary_after` describe the document as `{sha256, bytes,
type, size}`; the SHA-256 is over canonicalized JSON (sorted object keys,
compact separators, UTF-8). On rejection both fields are identical because the
store was not touched.

`apply_ops(document, ops)` is the lower-level helper: it works on a deep copy
and raises a `PatchError` subclass (with `.op_index` / `.op_name` attached)
instead of returning a record.

## Semantics implemented

### JSON Pointer (RFC 6901)

* `""` addresses the whole document; `"/"` addresses the member named `""`.
* Tokens are decoded `~1` → `/` then `~0` → `~` (in that order, so `~01` → `~1`).
* A bare `~` or an escape like `~2` is rejected.
* Missing object members and out-of-range array indices are errors, never
  silently created. `null` is a real value and is distinct from "missing".

### Array indices

* Legal forms are `0` and decimal digits with no leading zero. `01`, `00`, `-1`
  and non-ASCII digits are rejected.
* `-` (append) is accepted only as `add`'s target, as `copy`'s target, and as
  `move`'s **destination** (the destination uses add semantics). It is illegal
  as a `remove`/`replace`/`test` target and inside any `from` pointer.
* `add` into an array **inserts**; the index may be `len(array)` or `-`, but not
  greater.

### Operations

| op | rules |
| --- | --- |
| `add` | Replaces an existing object member; inserts into an array; may replace the root. Parent container must already exist. |
| `remove` | Target must exist. Root removal is explicitly **not supported** and is rejected. |
| `replace` | Target must exist; may replace the root. |
| `move` | Source must exist. The source is removed first, then `path` is resolved against the **already-modified** document — so same-array moves use post-removal indices and are not re-adjusted. Moving `from` the same pointer as `path` is a no-op. Moving a value into its own strict descendant is rejected. |
| `copy` | Deep copy of the source; the two sides are fully independent afterwards. May copy the root into a member. |
| `test` | Structural JSON equality (see below); the target must exist (`null` matches only `null`). |

`move`/`copy` require a string `from`; every operation requires a string `path`;
unknown operation names and non-object operations are rejected.

### `test` equality

* Object member order is irrelevant; array order is significant.
* Numbers compare numerically: `1 == 1.0`, `0 == -0.0`, large integers compare
  exactly.
* Booleans are **not** numbers: `true != 1`, `false != 0`.
* Strings compare exactly — no Unicode normalization or case folding.
* `null` equals only `null`; missing ≠ `null`.

### Atomicity

Every batch runs against a deep copy of the document. If any op fails, the
copy is discarded, the original is left untouched (so `summary_after ==
summary_before`), and the failure is reported with the 0-based `op_index`, the
offending `path`, the `from` pointer when relevant, a machine-readable
`reason`, and a message. Batches with unknown `doc_id`, malformed JSON lines or
a malformed `ops` array are likewise rejected without aborting the run.

### Reason codes

`invalid_pointer`, `missing_target`, `invalid_operation`, `invalid_index`,
`index_out_of_range`, `parent_not_container`, `move_into_descendant`,
`root_removal_unsupported`, `test_failed`, `invalid_batch`, `unknown_doc`.

## Fixture walkthrough

| batch | doc | outcome | why |
| --- | --- | --- | --- |
| p1 | d1 | accepted | move `A` from `/items/0` to `/items/2` after removal |
| p2 | d1 | accepted | deep copy to `/-`, then edits; `~1`/`~0` keys |
| p3 | d1 | rejected | `add /leak` succeeds, then `test /optional == false` fails → rollback |
| p4 | d2 | accepted | append `null`, replace status |
| p5 | d1 | rejected | move ancestor `/items` into descendant `/items/0/child` |
| p6 | d1 | accepted | numeric `test` `3.0 == 3`, remove `null` member |
| p7 | d2 | accepted | replace the root with `[1, 2, 3]` |
| p8 | d2 | rejected | `/01` on an array has a leading-zero index |

Final d1: `items = [{"sku":"B","qty":20},{"sku":"C","qty":3},{"sku":"A","qty":1},{"sku":"B","qty":2}]`
plus `a/b = {"~tag":"new"}`; final d2: `[1, 2, 3]`.

## Tests

```bash
python3 -m unittest discover -s tests -v
# or, if pytest is installed:
python3 -m pytest tests -q
```

The suite covers pointers and escapes, index validation, every operation,
`null` vs missing, deep-copy isolation, move-into-descendant rejection,
root replace/remove, batch atomicity and rollback, summary chaining, the full
fixture run, the CLI, and **composed operation sequences** (move-then-test,
copy-then-mutate, add/move/remove chains, root replace followed by member ops,
late failure rolling back earlier successful ops).

`ParityTests` cross-checks a handful of results against the independent
`jsonpatch` package. It is skipped automatically when that package is not
installed:

```bash
pip install --target /tmp/jpdeps jsonpatch
PYTHONPATH=/tmp/jpdeps python3 -m unittest discover -s tests -v
```

## Layout

```
docchange/
  __init__.py      public exports
  __main__.py      python -m docchange
  cli.py           fixtures loading, run loop, output writing
  compare.py       JSON structural equality for test
  engine.py        RFC 6902 operations + atomic apply_batch
  errors.py        error types and reason codes
  jsonio.py        strict JSON loading (rejects NaN/Infinity)
  pointer.py       RFC 6901 parsing, escaping and index validation
tests/
  test_docchange.py
fixtures/
  initial.json, patches.jsonl
```

## Out of scope

Offline document data production only: no patch generation, no collaborative
editing, no three-way merge/versioning, no persistence or retry/replay of batch
ids (they are guaranteed unique and used only for locating a record), and no
Docker. Input numbers are finite.
