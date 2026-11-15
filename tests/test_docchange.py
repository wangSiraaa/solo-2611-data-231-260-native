"""Tests for the document-change ingester.

Run with either::

    python -m unittest discover -s tests -v
    python -m pytest tests -q
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docchange import (  # noqa: E402
    ArrayIndexOutOfRange,
    InvalidIndex,
    InvalidOperation,
    InvalidPointer,
    MissingTarget,
    MoveIntoDescendant,
    PatchError,
    RootRemovalUnsupported,
    TestFailed,
    UnknownDocument,
    apply_batch,
    apply_ops,
    json_equal,
    parse_pointer,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(ROOT, "fixtures")


def run_ops(document, ops):
    return apply_ops(document, ops)


class PointerTests(unittest.TestCase):
    def test_empty_pointer_is_root(self):
        self.assertEqual(parse_pointer(""), [])

    def test_escapes_decode_in_rfc_order(self):
        # "~01" must become "~1", not "/"; "~1" becomes "/".
        self.assertEqual(parse_pointer("/a~1b/~0tag"), ["a/b", "~tag"])
        self.assertEqual(parse_pointer("/~01"), ["~1"])
        self.assertEqual(parse_pointer("/~10"), ["/0"])

    def test_slash_alone_is_empty_member_name(self):
        self.assertEqual(parse_pointer("/"), [""])

    def test_invalid_escapes(self):
        for bad in ("/a~2b", "/a~", "/~"):
            with self.assertRaises(InvalidPointer):
                parse_pointer(bad)

    def test_pointer_must_be_empty_or_slash_prefixed(self):
        with self.assertRaises(InvalidPointer):
            parse_pointer("a/b")


class AddTests(unittest.TestCase):
    def test_root_add_replaces_whole_document(self):
        result = run_ops({"x": 1}, [{"op": "add", "path": "", "value": [1, 2]}])
        self.assertEqual(result, [1, 2])

    def test_object_add_existing_member_replaces(self):
        result = run_ops({"a": 1}, [{"op": "add", "path": "/a", "value": 2}])
        self.assertEqual(result, {"a": 2})

    def test_object_add_new_member(self):
        result = run_ops({}, [{"op": "add", "path": "/a", "value": None}])
        self.assertEqual(result, {"a": None})

    def test_array_add_inserts(self):
        result = run_ops([10, 20, 30], [{"op": "add", "path": "/1", "value": 15}])
        self.assertEqual(result, [10, 15, 20, 30])

    def test_array_add_at_length_is_append(self):
        result = run_ops([1, 2], [{"op": "add", "path": "/2", "value": 3}])
        self.assertEqual(result, [1, 2, 3])

    def test_array_add_dash_appends(self):
        result = run_ops([1, 2], [{"op": "add", "path": "/-", "value": 3}])
        self.assertEqual(result, [1, 2, 3])

    def test_array_add_beyond_length_rejected(self):
        with self.assertRaises(ArrayIndexOutOfRange):
            run_ops([1], [{"op": "add", "path": "/2", "value": 9}])

    def test_dash_key_in_object_is_ordinary(self):
        result = run_ops({}, [{"op": "add", "path": "/-", "value": 1}])
        self.assertEqual(result, {"-": 1})

    def test_leading_zero_index_rejected(self):
        for pointer in ("/01", "/00", "/007"):
            with self.assertRaises(InvalidIndex):
                run_ops([], [{"op": "add", "path": pointer, "value": 1}])

    def test_negative_index_rejected(self):
        with self.assertRaises(InvalidIndex):
            run_ops([1], [{"op": "remove", "path": "/-1"}])

    def test_unicode_digit_index_rejected(self):
        with self.assertRaises(InvalidIndex):
            run_ops([1], [{"op": "remove", "path": "/١"}])

    def test_add_parent_must_exist(self):
        with self.assertRaises(InvalidPointer):
            run_ops({}, [{"op": "add", "path": "/x/y", "value": 1}])

    def test_add_missing_value_member(self):
        with self.assertRaises(InvalidOperation):
            run_ops({}, [{"op": "add", "path": "/a"}])

    def test_unknown_operation_reports_path(self):
        with self.assertRaises(InvalidOperation) as ctx:
            run_ops({}, [{"op": "merge", "path": "/a", "value": 1}])
        self.assertEqual(ctx.exception.path, "/a")

    def test_missing_path_member(self):
        with self.assertRaises(InvalidOperation):
            run_ops({}, [{"op": "add", "value": 1}])

    def test_non_string_path(self):
        with self.assertRaises(InvalidOperation):
            run_ops({}, [{"op": "add", "path": 1, "value": 1}])

    def test_non_object_op(self):
        with self.assertRaises(InvalidOperation):
            run_ops({}, ["not an op"])

    def test_ops_must_be_a_list(self):
        with self.assertRaises(InvalidOperation):
            run_ops({}, {"op": "add", "path": "/a", "value": 1})


class RemoveReplaceTestTests(unittest.TestCase):
    def test_root_remove_rejected(self):
        with self.assertRaises(RootRemovalUnsupported):
            run_ops({"a": 1}, [{"op": "remove", "path": ""}])

    def test_remove_missing_member_rejected(self):
        with self.assertRaises(MissingTarget):
            run_ops({"a": 1}, [{"op": "remove", "path": "/b"}])

    def test_remove_dash_rejected(self):
        with self.assertRaises(InvalidIndex):
            run_ops([1], [{"op": "remove", "path": "/-"}])

    def test_replace_requires_target(self):
        with self.assertRaises(MissingTarget):
            run_ops({"a": 1}, [{"op": "replace", "path": "/b", "value": 2}])
        with self.assertRaises(ArrayIndexOutOfRange):
            run_ops([1], [{"op": "replace", "path": "/1", "value": 2}])

    def test_replace_root(self):
        result = run_ops({"a": 1}, [{"op": "replace", "path": "", "value": "x"}])
        self.assertEqual(result, "x")

    def test_replace_dash_rejected(self):
        with self.assertRaises(InvalidIndex):
            run_ops([1], [{"op": "replace", "path": "/-", "value": 2}])

    def test_test_requires_target(self):
        with self.assertRaises(MissingTarget):
            run_ops({"a": None}, [{"op": "test", "path": "/b", "value": None}])

    def test_test_null_matches_null(self):
        result = run_ops({"a": None}, [{"op": "test", "path": "/a", "value": None}])
        self.assertEqual(result, {"a": None})

    def test_missing_vs_null_distinguished(self):
        # Adding an explicit null must not look like an existing member.
        result = run_ops({}, [{"op": "add", "path": "/a", "value": None}])
        self.assertEqual(result, {"a": None})
        with self.assertRaises(MissingTarget):
            run_ops({"a": None}, [{"op": "remove", "path": "/b"}])

    def test_test_dash_rejected(self):
        with self.assertRaises(InvalidIndex):
            run_ops([1], [{"op": "test", "path": "/-", "value": 1}])

    def test_navigation_through_scalar_rejected(self):
        with self.assertRaises(InvalidPointer):
            run_ops({"a": 1}, [{"op": "test", "path": "/a/b", "value": 1}])

    def test_pointer_with_tilde_slash_keys(self):
        document = {"a/b": {"~tag": "old"}}
        result = run_ops(
            document, [{"op": "replace", "path": "/a~1b/~0tag", "value": "new"}]
        )
        self.assertEqual(result, {"a/b": {"~tag": "new"}})


class TestComparisonTests(unittest.TestCase):
    def test_numeric_int_float_equal(self):
        self.assertTrue(json_equal(1, 1.0))
        self.assertTrue(json_equal(1.0, 1))
        self.assertTrue(json_equal(0, -0.0))

    def test_boolean_is_not_number(self):
        self.assertFalse(json_equal(True, 1))
        self.assertFalse(json_equal(False, 0))
        self.assertTrue(json_equal(True, True))

    def test_object_order_ignored(self):
        self.assertTrue(json_equal({"a": 1, "b": 2}, {"b": 2, "a": 1}))

    def test_array_order_matters(self):
        self.assertFalse(json_equal([1, 2], [2, 1]))

    def test_strings_not_normalized(self):
        self.assertFalse(json_equal("é", "é"))
        self.assertFalse(json_equal("A", "a"))

    def test_large_integers(self):
        self.assertTrue(json_equal(2 ** 53 + 1, 2 ** 53 + 1))
        self.assertFalse(json_equal(2 ** 53 + 1, float(2 ** 53 + 1)))

    def test_type_confusion(self):
        self.assertFalse(json_equal(None, False))
        self.assertFalse(json_equal(None, 0))
        self.assertFalse(json_equal(None, ""))
        self.assertFalse(json_equal([], {}))
        self.assertFalse(json_equal("1", 1))
        self.assertFalse(json_equal({}, None))


class MoveCopyTests(unittest.TestCase):
    def test_move_within_array_backwards_uses_post_removal_index(self):
        # [A,B,C] move /0 -> /2: remove A -> [B,C], insert at 2 -> [B,C,A].
        result = run_ops(
            ["A", "B", "C"], [{"op": "move", "from": "/0", "path": "/2"}]
        )
        self.assertEqual(result, ["B", "C", "A"])

    def test_move_within_array_forwards_uses_post_removal_index(self):
        # [A,B,C] move /2 -> /0: remove C -> [A,B], insert at 0 -> [C,A,B].
        result = run_ops(
            ["A", "B", "C"], [{"op": "move", "from": "/2", "path": "/0"}]
        )
        self.assertEqual(result, ["C", "A", "B"])

    def test_move_no_shifting_to_preserve_original_position(self):
        # Moving /1 to /2 on [A,B,C]: remove B -> [A,C], insert at 2 -> [A,C,B].
        result = run_ops(
            ["A", "B", "C"], [{"op": "move", "from": "/1", "path": "/2"}]
        )
        self.assertEqual(result, ["A", "C", "B"])

    def test_move_to_same_pointer_is_noop(self):
        document = {"a": [1, 2]}
        snapshot = copy.deepcopy(document)
        result = run_ops(document, [{"op": "move", "from": "/a", "path": "/a"}])
        self.assertEqual(result, snapshot)
        self.assertEqual(document, snapshot)  # input untouched too

    def test_move_same_pointer_missing_source_still_fails(self):
        with self.assertRaises(MissingTarget):
            run_ops({}, [{"op": "move", "from": "/x", "path": "/x"}])

    def test_move_into_descendant_rejected(self):
        with self.assertRaises(MoveIntoDescendant):
            run_ops(
                {"items": [{"x": 1}]},
                [{"op": "move", "from": "/items", "path": "/items/0/child"}],
            )

    def test_move_into_itself_is_nested_descendant(self):
        with self.assertRaises(MoveIntoDescendant):
            run_ops(
                {"a": {"b": 1}},
                [{"op": "move", "from": "/a", "path": "/a/b/c"}],
            )

    def test_move_object_between_parents(self):
        result = run_ops(
            {"x": {"v": 1}, "y": {}},
            [{"op": "move", "from": "/x", "path": "/y/x"}],
        )
        self.assertEqual(result, {"y": {"x": {"v": 1}}})

    def test_move_append_dash(self):
        result = run_ops(
            {"a": [1], "b": 2},
            [{"op": "move", "from": "/b", "path": "/a/-"}],
        )
        self.assertEqual(result, {"a": [1, 2]})

    def test_move_dash_index_after_removal(self):
        # [1,2,3,4] move /1 -> /-: remove 2 -> [1,3,4], append at end.
        result = run_ops(
            [1, 2, 3, 4], [{"op": "move", "from": "/1", "path": "/-"}]
        )
        self.assertEqual(result, [1, 3, 4, 2])

    def test_move_dest_beyond_post_removal_length(self):
        # [1,2] move /0 -> /2: remove 1 -> [2] (length 1); insert index 2 is
        # out of range, so the operation fails rather than silently appending.
        with self.assertRaises(ArrayIndexOutOfRange):
            run_ops([1, 2], [{"op": "move", "from": "/0", "path": "/2"}])

    def test_move_missing_source_rejected(self):
        with self.assertRaises(MissingTarget):
            run_ops({}, [{"op": "move", "from": "/x", "path": "/y"}])

    def test_move_missing_from_member(self):
        with self.assertRaises(InvalidOperation):
            run_ops({}, [{"op": "move", "path": "/y"}])

    def test_move_from_root_to_descendant_rejected(self):
        with self.assertRaises(MoveIntoDescendant):
            run_ops({"a": 1}, [{"op": "move", "from": "", "path": "/a/x"}])

    def test_move_root_to_root_is_noop(self):
        document = {"a": 1}
        result = run_ops(document, [{"op": "move", "from": "", "path": ""}])
        self.assertEqual(result, {"a": 1})

    def test_copy_is_deep(self):
        document = {"a": {"nested": [1, 2]}, "b": None}
        result = run_ops(document, [{"op": "copy", "from": "/a", "path": "/b"}])
        self.assertEqual(result, {"a": {"nested": [1, 2]}, "b": {"nested": [1, 2]}})
        # Mutating one side must not change the other.
        result["a"]["nested"].append(3)
        self.assertEqual(result["b"], {"nested": [1, 2]})
        # The source document itself was not mutated by apply_ops.
        self.assertEqual(document, {"a": {"nested": [1, 2]}, "b": None})

    def test_copy_missing_source_rejected(self):
        with self.assertRaises(MissingTarget):
            run_ops({}, [{"op": "copy", "from": "/x", "path": "/y"}])

    def test_copy_dash_appends(self):
        result = run_ops({"a": [1]}, [{"op": "copy", "from": "/a/0", "path": "/a/-"}])
        self.assertEqual(result, {"a": [1, 1]})

    def test_copy_root_to_member(self):
        result = run_ops(
            {"a": 1, "snap": None}, [{"op": "copy", "from": "", "path": "/snap"}]
        )
        self.assertEqual(result, {"a": 1, "snap": {"a": 1, "snap": None}})

    def test_move_from_dash_rejected(self):
        with self.assertRaises(InvalidIndex):
            run_ops([1], [{"op": "move", "from": "/-", "path": "/0"}])


class AtomicBatchTests(unittest.TestCase):
    def test_apply_ops_does_not_mutate_input_even_on_failure(self):
        document = {"a": 1, "items": [{"v": 1}]}
        snapshot = copy.deepcopy(document)
        with self.assertRaises(TestFailed):
            run_ops(
                document,
                [
                    {"op": "add", "path": "/leak", "value": True},
                    {"op": "test", "path": "/a", "value": 2},
                ],
            )
        self.assertEqual(document, snapshot)

    def test_failed_test_does_not_leak_added_field(self):
        result = apply_batch(
            {"d": {"a": 1}},
            {
                "batch_id": "b1",
                "doc_id": "d",
                "ops": [
                    {"op": "add", "path": "/leak", "value": True},
                    {"op": "test", "path": "/a", "value": 2},
                ],
            },
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["op_index"], 1)
        self.assertEqual(result["path"], "/a")
        self.assertEqual(result["summary_after"], result["summary_before"])

    def test_unknown_document_rejected_and_untouched(self):
        store = {"d": {"a": 1}}
        result = apply_batch(
            store,
            {"batch_id": "b", "doc_id": "nope", "ops": [{"op": "add", "path": "/x", "value": 1}]},
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["reason"], UnknownDocument.reason)
        self.assertEqual(store, {"d": {"a": 1}})

    def test_malformed_batch_records(self):
        store = {"d": {"a": 1}}
        cases = [
            (None, "invalid_batch"),
            ([], "invalid_batch"),
            ({"doc_id": "d", "ops": []}, "invalid_batch"),
            ({"batch_id": "b", "doc_id": "d"}, "invalid_batch"),
            ({"batch_id": "b", "doc_id": "d", "ops": {}}, "invalid_batch"),
            ({"batch_id": True, "doc_id": "d", "ops": []}, "invalid_batch"),
            ({"batch_id": "b", "doc_id": 3, "ops": []}, "invalid_batch"),
        ]
        for batch, reason in cases:
            result = apply_batch(store, batch)
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["reason"], reason)

    def test_accepted_batch_commits_all_ops(self):
        store = {"d": {"a": [1, 2], "b": "x"}}
        result = apply_batch(
            store,
            {
                "batch_id": "ok",
                "doc_id": "d",
                "ops": [
                    {"op": "add", "path": "/a/1", "value": 1.5},
                    {"op": "remove", "path": "/b"},
                    {"op": "copy", "from": "/a/0", "path": "/c"},
                ],
            },
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(store["d"], {"a": [1, 1.5, 2], "c": 1})
        self.assertNotEqual(
            result["summary_before"]["sha256"], result["summary_after"]["sha256"]
        )

    def test_rejected_batch_summary_equals_pre_state(self):
        store = {"d": {"a": 1}}
        before = {"a": 1}
        result = apply_batch(
            store,
            {"batch_id": "r", "doc_id": "d", "ops": [{"op": "remove", "path": "/zz"}]},
        )
        self.assertEqual(store["d"], before)
        self.assertEqual(result["summary_after"], result["summary_before"])

    def test_bad_op_in_middle_reports_index(self):
        store = {"d": [1, 2, 3]}
        result = apply_batch(
            store,
            {
                "batch_id": "m",
                "doc_id": "d",
                "ops": [
                    {"op": "remove", "path": "/0"},
                    {"op": "add", "path": "/01", "value": 9},
                    {"op": "add", "path": "/-", "value": 9},
                ],
            },
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["op_index"], 1)
        self.assertEqual(result["path"], "/01")
        self.assertEqual(result["reason"], InvalidIndex.reason)
        self.assertEqual(store["d"], [1, 2, 3])

    def test_apply_ops_attaches_context(self):
        with self.assertRaises(TestFailed) as ctx:
            run_ops({"a": 1}, [{"op": "test", "path": "/a", "value": 2}])
        self.assertEqual(ctx.exception.op_index, 0)
        self.assertEqual(ctx.exception.op_name, "test")


class CompositionTests(unittest.TestCase):
    """Sequences, not isolated branches: state feeds the next operation."""

    def test_move_then_test_on_new_positions(self):
        result = run_ops(
            {"items": [{"id": "A"}, {"id": "B"}, {"id": "C"}]},
            [
                {"op": "move", "from": "/items/0", "path": "/items/2"},
                {"op": "test", "path": "/items/2/id", "value": "A"},
                {"op": "test", "path": "/items/0/id", "value": "B"},
            ],
        )
        self.assertEqual([x["id"] for x in result["items"]], ["B", "C", "A"])

    def test_copy_then_mutate_copy_only(self):
        result = run_ops(
            {"src": {"v": 1}},
            [
                {"op": "copy", "from": "/src", "path": "/dst"},
                {"op": "replace", "path": "/dst/v", "value": 2},
            ],
        )
        self.assertEqual(result, {"src": {"v": 1}, "dst": {"v": 2}})

    def test_add_then_move_then_remove_chain(self):
        result = run_ops(
            {"seq": [1, 2]},
            [
                {"op": "add", "path": "/seq/1", "value": 99},
                {"op": "move", "from": "/seq/1", "path": "/seq/0"},
                {"op": "remove", "path": "/seq/2"},
            ],
        )
        # [1,2] -> [1,99,2] -> [99,1,2] -> [99,1]
        self.assertEqual(result, {"seq": [99, 1]})

    def test_remove_dash_in_sequence_rolls_back_earlier_add(self):
        # Root replacement plus a later illegal operation: the earlier add must
        # not survive, which only shows up when checking the *store*, not a
        # fresh document.
        snack = {"d": {"items": [1, 2]}}
        result = apply_batch(
            snack,
            {
                "batch_id": "seq",
                "doc_id": "d",
                "ops": [
                    {"op": "add", "path": "/newkey", "value": 1},
                    {"op": "remove", "path": "/items/-"},
                ],
            },
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["op_index"], 1)
        self.assertEqual(result["reason"], InvalidIndex.reason)
        self.assertEqual(snack["d"], {"items": [1, 2]})

    def test_test_observes_prior_operation_result(self):
        result = run_ops(
            {"a": 1},
            [
                {"op": "replace", "path": "/a", "value": 1.0},
                {"op": "test", "path": "/a", "value": 1},
            ],
        )
        self.assertEqual(result, {"a": 1.0})

    def test_nested_special_char_roundtrip_sequence(self):
        result = run_ops(
            {"a/b": {"~tag": "old"}},
            [
                {"op": "replace", "path": "/a~1b/~0tag", "value": "new"},
                {"op": "test", "path": "/a~1b/~0tag", "value": "new"},
                {"op": "copy", "from": "/a~1b/~0tag", "path": "/a~1b/~0copy"},
            ],
        )
        self.assertEqual(result, {"a/b": {"~tag": "new", "~copy": "new"}})

    def test_move_across_containers_then_test(self):
        result = run_ops(
            {"left": [1, 2], "right": []},
            [
                {"op": "move", "from": "/left/1", "path": "/right/-"},
                {"op": "test", "path": "/right/0", "value": 2},
                {"op": "remove", "path": "/left/0"},
            ],
        )
        self.assertEqual(result, {"left": [], "right": [2]})

    def test_root_replace_then_member_ops(self):
        result = run_ops(
            {"old": 1},
            [
                {"op": "replace", "path": "", "value": {"new": [1, 2]}},
                {"op": "add", "path": "/new/-", "value": 3},
                {"op": "test", "path": "/new/2", "value": 3},
            ],
        )
        self.assertEqual(result, {"new": [1, 2, 3]})

    def test_failure_late_in_sequence_rolls_back_earlier_conflicts(self):
        store = {"d": {"items": [1, 2, 3]}}
        result = apply_batch(
            store,
            {
                "batch_id": "roll",
                "doc_id": "d",
                "ops": [
                    {"op": "remove", "path": "/items/0"},
                    {"op": "replace", "path": "/items/0", "value": 20},
                    {"op": "move", "from": "/items/0", "path": "/items/0"},
                    {"op": "test", "path": "/items/2", "value": 3},
                ],
            },
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["op_index"], 3)
        self.assertEqual(result["path"], "/items/2")
        self.assertEqual(result["reason"], "index_out_of_range")
        # The store still holds the pre-batch document, not the partially
        # applied remove/replace/move sequence.
        self.assertEqual(store["d"], {"items": [1, 2, 3]})


class CliInputTests(unittest.TestCase):
    def test_malformed_jsonl_line_is_rejected_and_run_continues(self):
        from docchange.cli import run

        with tempfile.TemporaryDirectory() as tmp:
            initial = os.path.join(tmp, "initial.json")
            patches = os.path.join(tmp, "patches.jsonl")
            with open(initial, "w", encoding="utf-8") as f:
                json.dump({"docs": {"d": {"a": 1}}}, f)
            with open(patches, "w", encoding="utf-8") as f:
                f.write('{"batch_id": "ok", "doc_id": "d", "ops": [{"op": "add", "path": "/b", "value": 2}]}\n')
                f.write("this is not json\n")
                f.write('{"batch_id": "tail", "doc_id": "d", "ops": [{"op": "test", "path": "/b", "value": 2}]}\n')
            docs, results = run(initial, patches, os.path.join(tmp, "out"))
        self.assertEqual(docs["docs"]["d"], {"a": 1, "b": 2})
        self.assertEqual(
            [(r["batch_id"], r["status"]) for r in results],
            [("ok", "accepted"), (None, "rejected"), ("tail", "accepted")],
        )
        self.assertEqual(results[1]["reason"], "invalid_batch")
        self.assertEqual(results[1]["line"], 2)

    def test_blank_lines_are_skipped(self):
        from docchange.cli import run

        with tempfile.TemporaryDirectory() as tmp:
            initial = os.path.join(tmp, "initial.json")
            patches = os.path.join(tmp, "patches.jsonl")
            with open(initial, "w", encoding="utf-8") as f:
                json.dump({"docs": {"d": {}}}, f)
            with open(patches, "w", encoding="utf-8") as f:
                f.write("\n  \n")
            docs, results = run(initial, patches, os.path.join(tmp, "out"))
        self.assertEqual(results, [])
        self.assertEqual(docs, {"docs": {"d": {}}})

    def test_initial_document_with_non_finite_number_is_rejected(self):
        from docchange.cli import load_initial

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "initial.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"docs": {"d": {"x": 1e400}}}')
            with self.assertRaises(ValueError):
                load_initial(path)

    def test_nan_and_infinity_tokens_rejected(self):
        from docchange.jsonio import loads

        for text in ('{"x": NaN}', '{"x": Infinity}', '{"x": -Infinity}'):
            with self.assertRaises(ValueError):
                loads(text)

    def test_unsupported_batch_id_is_logged_as_null(self):
        from docchange.cli import run

        with tempfile.TemporaryDirectory() as tmp:
            initial = os.path.join(tmp, "initial.json")
            patches = os.path.join(tmp, "patches.jsonl")
            with open(initial, "w", encoding="utf-8") as f:
                json.dump({"docs": {"d": {}}}, f)
            with open(patches, "w", encoding="utf-8") as f:
                f.write('{"batch_id": [1], "doc_id": "d", "ops": []}\n')
            docs, results = run(initial, patches, os.path.join(tmp, "out"))
            with open(os.path.join(tmp, "out", "batches.jsonl"), encoding="utf-8") as f:
                records = [json.loads(line) for line in f]
        self.assertEqual(results[0]["status"], "rejected")
        self.assertEqual(results[0]["reason"], "invalid_batch")
        self.assertIsNone(records[0]["batch_id"])
        self.assertEqual(docs, {"docs": {"d": {}}})

    def test_unknown_document_has_no_summary_and_store_untouched(self):
        from docchange.cli import run

        with tempfile.TemporaryDirectory() as tmp:
            initial = os.path.join(tmp, "initial.json")
            patches = os.path.join(tmp, "patches.jsonl")
            with open(initial, "w", encoding="utf-8") as f:
                json.dump({"docs": {"d": {"a": 1}}}, f)
            with open(patches, "w", encoding="utf-8") as f:
                f.write('{"batch_id": "u", "doc_id": "nope", "ops": []}\n')
            docs, results = run(initial, patches, os.path.join(tmp, "out"))
        self.assertEqual(results[0]["status"], "rejected")
        self.assertEqual(results[0]["reason"], "unknown_doc")
        self.assertIsNone(results[0]["summary_before"])
        self.assertEqual(docs, {"docs": {"d": {"a": 1}}})


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from docchange.cli import run

        cls.tmp = tempfile.TemporaryDirectory()
        doc_path = os.path.join(FIXTURES, "initial.json")
        patch_path = os.path.join(FIXTURES, "patches.jsonl")
        cls.documents, cls.results = run(doc_path, patch_path, cls.tmp.name)
        cls.out = cls.tmp.name

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_final_documents(self):
        docs = self.documents["docs"]
        self.assertEqual(
            docs["d1"],
            {
                "items": [
                    {"sku": "B", "qty": 20},
                    {"sku": "C", "qty": 3},
                    {"sku": "A", "qty": 1},
                    {"sku": "B", "qty": 2},
                ],
                "a/b": {"~tag": "new"},
            },
        )
        self.assertEqual(docs["d2"], [1, 2, 3])

    def test_batch_statuses_and_locators(self):
        by_id = {r["batch_id"]: r for r in self.results}
        expected = {
            "p1": "accepted",
            "p2": "accepted",
            "p3": "rejected",
            "p4": "accepted",
            "p5": "rejected",
            "p6": "accepted",
            "p7": "accepted",
            "p8": "rejected",
        }
        self.assertEqual({k: v["status"] for k, v in by_id.items()}, expected)

        self.assertEqual(by_id["p3"]["op_index"], 1)
        self.assertEqual(by_id["p3"]["path"], "/optional")
        self.assertEqual(by_id["p3"]["reason"], "test_failed")
        self.assertEqual(by_id["p5"]["op_index"], 0)
        self.assertEqual(by_id["p5"]["path"], "/items/0/child")
        self.assertEqual(by_id["p5"]["from"], "/items")
        self.assertEqual(by_id["p5"]["reason"], "move_into_descendant")
        self.assertEqual(by_id["p8"]["op_index"], 0)
        self.assertEqual(by_id["p8"]["path"], "/01")
        self.assertEqual(by_id["p8"]["reason"], "invalid_index")

    def test_rejected_summaries_equal_pre_state(self):
        for result in self.results:
            if result["status"] == "rejected":
                self.assertEqual(
                    result["summary_after"], result["summary_before"], result
                )

    def test_accepted_chains_summaries(self):
        # The next batch's "before" must equal the previous accepted batch's
        # "after"; rejections do not move the state.
        last = None
        for result in self.results:
            if result["status"] == "accepted":
                if last is not None and last["doc_id"] == result["doc_id"]:
                    self.assertEqual(result["summary_before"], last["summary_after"])
                last = result
            elif last is not None and last["doc_id"] == result["doc_id"]:
                self.assertEqual(result["summary_before"], last["summary_after"])

    def test_output_files_written(self):
        with open(os.path.join(self.out, "documents.json"), encoding="utf-8") as f:
            written = json.load(f)
        self.assertEqual(written["docs"], self.documents["docs"])
        with open(os.path.join(self.out, "batches.jsonl"), encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        self.assertEqual(len(lines), 8)
        self.assertEqual(
            [line["batch_id"] for line in lines],
            ["p1", "p2", "p3", "p4", "p5", "p6", "p7", "p8"],
        )

    def test_cli_exit_code_and_stdout(self):
        output = os.path.join(self.tmp.name, "cli-out")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "docchange",
                os.path.join(FIXTURES, "initial.json"),
                os.path.join(FIXTURES, "patches.jsonl"),
                "-o",
                output,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(os.path.exists(os.path.join(output, "documents.json")))


class ParityTests(unittest.TestCase):
    """Compare against an independent RFC 6902 implementation when installed."""

    @classmethod
    def setUpClass(cls):
        try:
            import jsonpatch  # noqa: F401
        except ImportError:  # pragma: no cover - depends on environment
            raise unittest.SkipTest("jsonpatch is not installed")
        cls.jsonpatch = __import__("jsonpatch")

    def _ours(self, document, ops):
        return run_ops(document, ops)

    def _theirs(self, document, ops):
        return self.jsonpatch.JsonPatch(copy.deepcopy(ops)).apply(document, in_place=False)

    def assert_parity(self, document, ops):
        ours = self._ours(copy.deepcopy(document), copy.deepcopy(ops))
        theirs = self._theirs(copy.deepcopy(document), copy.deepcopy(ops))
        self.assertTrue(json_equal(ours, theirs), (ours, theirs))

    def test_parity_cases(self):
        cases = [
            ([10, 20, 30], [{"op": "move", "from": "/0", "path": "/2"}]),
            ({"a": 1}, [{"op": "add", "path": "/a", "value": 2}]),
            ({"a/b": {"~tag": 1}}, [{"op": "test", "path": "/a~1b/~0tag", "value": 1}]),
            ({"a": [1]}, [{"op": "copy", "from": "/a", "path": "/a/-"}]),
            ({"a": {"b": 1}}, [{"op": "move", "from": "/a", "path": "/c"}]),
        ]
        for document, ops in cases:
            self.assert_parity(document, ops)


if __name__ == "__main__":
    unittest.main()
