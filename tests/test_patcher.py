"""Tests for the byte-faithful CSV patcher.

Run with ``python -m unittest discover -s tests -v`` (pytest is not required).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from csv_fidelity import PatchError, PatchErrorCode, apply_patch, verify
from csv_fidelity.patcher import render_field
from csv_fidelity.scanner import scan_document

FIXTURES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures")
SOURCE = os.path.join(FIXTURES, "source.csv")


def read_fixture(name: str) -> bytes:
    with open(os.path.join(FIXTURES, name), "rb") as handle:
        return handle.read()


def load_patch(name: str) -> dict:
    with open(os.path.join(FIXTURES, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def make_patch(source: bytes, changes: list[dict]) -> dict:
    return {"source_sha256": sha(source), "changes": changes}


def logical_rows(data: bytes) -> list[list[str]]:
    text = data.decode("utf-8")
    if text.startswith("﻿"):
        text = text[1:]
    return [list(r) for r in csv.reader(io.StringIO(text, newline=""), strict=True)]


class FixtureTests(unittest.TestCase):
    def setUp(self):
        self.source = read_fixture("source.csv")

    def test_scanner_offsets_are_byte_offsets_for_cjk(self):
        doc = scan_document(self.source)
        cell = doc.records[2].cells[1]
        self.assertTrue(cell.quoted)
        self.assertEqual(self.source[cell.start : cell.end], b'"\xe4\xb8\xad\xe6\x96\x87"')
        self.assertEqual(cell.value, "中文")
        # The quoted lexeme including quotes is 8 bytes, not 4 characters.
        self.assertEqual(cell.end - cell.start, 8)
        self.assertEqual(self.source[cell.start], ord('"'))

    def test_bom_excluded_and_mixed_terminators_preserved(self):
        doc = scan_document(self.source)
        self.assertEqual(doc.bom, b"\xef\xbb\xbf")
        self.assertEqual(self.source[doc.header_terminator[0] : doc.header_terminator[1]], b"\r\n")
        self.assertEqual(self.source[doc.records[0].terminator_start : doc.records[0].terminator_end], b"\n")
        self.assertEqual(self.source[doc.records[1].terminator_start : doc.records[1].terminator_end], b"\r\n")
        self.assertFalse(doc.last_record_terminated)

    def test_crlf_inside_quoted_field_is_not_a_record_terminator(self):
        doc = scan_document(self.source)
        value = doc.records[0].cells[2].value
        self.assertEqual(value, "first\r\nsecond")
        self.assertEqual(len(doc.records), 3)

    def test_quoted_escaped_quotes_decode(self):
        doc = scan_document(self.source)
        self.assertEqual(doc.records[1].cells[2].value, 'say "hi"')

    def test_main_patch_semantics_and_bytes(self):
        patch = load_patch("patch.json")
        result = apply_patch(self.source, patch)
        target = result["output"]

        rows = logical_rows(target)
        self.assertEqual(rows[0], ["id", "name", "note", "amount"])
        self.assertEqual(rows[1], ["1", "A, B", "new\nline", "10"])
        self.assertEqual(rows[2], ["2", 'B"ob', 'say "hi"', "20"])
        self.assertEqual(rows[3], ["3", "中文", "tail", "30"])

        # BOM, header, terminators and final-newline state survive.
        self.assertTrue(target.startswith(b"\xef\xbb\xbfid,\"name\",note,amount\r\n"))
        self.assertIn(b"1,\"A, B\",\"new\nline\",10\n", target)
        self.assertIn(b"2,\"B\"\"ob\",", target)
        self.assertTrue(target.endswith(b"3,\"\xe4\xb8\xad\xe6\x96\x87\",tail,30"))
        self.assertFalse(target.endswith(b"\n"))
        # Untouched fields keep their original lexeme (quoted name stays quoted).
        self.assertIn(b'"name"', target)
        self.assertIn(b'"say ""hi"""', target)

        verify(self.source, target, patch, result["map"])
        self.assertEqual(result["map"]["target_sha256"], sha(target))

    def test_quoted_old_field_stays_quoted_even_without_special_chars(self):
        # Fixture row 1 column 1 ("Bob") is unquoted, so "B\"ob" upgrades.
        # Here we assert the renderer contract directly.
        self.assertEqual(render_field("plain", True), b'"plain"')
        self.assertEqual(render_field('a"b', True), b'"a""b"')
        self.assertEqual(render_field("a,b", False), b'"a,b"')
        self.assertEqual(render_field("a\rb", False), b'"a\rb"')
        self.assertEqual(render_field("a\nb", False), b'"a\nb"')
        self.assertEqual(render_field("plain", False), b"plain")

    def test_empty_cell_ipython_insertion_and_zero_length_source_range(self):
        patch = load_patch("patch.json")
        result = apply_patch(self.source, patch)
        segments = result["map"]["segments"]
        zero = [
            s
            for s in segments
            if s["kind"] == "replace" and s["source_start"] == s["source_end"]
        ]
        self.assertEqual(len(zero), 1)
        self.assertEqual((zero[0]["cell"]["row"], zero[0]["cell"]["column"]), (2, 2))
        # Ordered and non-overlapping tile: previous target end == its start.
        index = segments.index(zero[0])
        self.assertEqual(segments[index - 1]["target_end"], zero[0]["target_start"])

    def test_wrong_old_value_rejected_whole_batch(self):
        with self.assertRaises(PatchError) as ctx:
            apply_patch(self.source, load_patch("wrong-old-value.json"))
        self.assertIs(ctx.exception.code, PatchErrorCode.WRONG_OLD_VALUE)

    def test_duplicate_cell_rejected_whole_batch(self):
        with self.assertRaises(PatchError) as ctx:
            apply_patch(self.source, load_patch("duplicate-cell.json"))
        self.assertIs(ctx.exception.code, PatchErrorCode.DUPLICATE_CELL)

    def test_sha_mismatch_rejected(self):
        patch = load_patch("patch.json")
        patch["source_sha256"] = "0" * 64
        with self.assertRaises(PatchError) as ctx:
            apply_patch(self.source, patch)
        self.assertIs(ctx.exception.code, PatchErrorCode.SHA256_MISMATCH)

    def test_source_is_not_mutated(self):
        before = read_fixture("source.csv")
        apply_patch(self.source, load_patch("patch.json"))
        self.assertEqual(read_fixture("source.csv"), before)


class MalformedTests(unittest.TestCase):
    def test_unclosed_quote_fixture(self):
        with self.assertRaises(PatchError) as ctx:
            scan_document(read_fixture("malformed.csv"))
        self.assertIs(ctx.exception.code, PatchErrorCode.UNCLOSED_QUOTE)

    def test_bare_quote(self):
        with self.assertRaises(PatchError) as ctx:
            scan_document(b'a,b\n1,ab"c\n')
        self.assertIs(ctx.exception.code, PatchErrorCode.BARE_QUOTE)

    def test_trailing_after_quote(self):
        with self.assertRaises(PatchError) as ctx:
            scan_document(b'a,b\n1,"x"y\n')
        self.assertIs(ctx.exception.code, PatchErrorCode.TRAILING_AFTER_QUOTE)

    def test_column_count_mismatch(self):
        with self.assertRaises(PatchError) as ctx:
            scan_document(b'a,b\n1,2,3\n')
        self.assertIs(ctx.exception.code, PatchErrorCode.COLUMN_COUNT_MISMATCH)

    def test_invalid_utf8(self):
        with self.assertRaises(PatchError) as ctx:
            scan_document(b'a,b\n1,\xff\n')
        self.assertIs(ctx.exception.code, PatchErrorCode.INVALID_UTF8)

    def test_lone_cr_is_a_preserved_record_terminator(self):
        # csv.reader(newline="") treats a lone CR as a terminator; the patcher
        # agrees and copies the terminator verbatim.
        data = b"a,b\r1,x\r2,y\n"
        doc = scan_document(data)
        self.assertEqual([c.value for c in doc.records[0].cells], ["1", "x"])
        self.assertEqual(data[doc.records[0].terminator_start : doc.records[0].terminator_end], b"\r")
        patch = make_patch(data, [{"row": 1, "column": 1, "expected": "y", "value": "z"}])
        result = apply_patch(data, patch)
        self.assertEqual(result["output"], b"a,b\r1,x\r2,z\n")
        self.assertEqual(logical_rows(result["output"]), [["a", "b"], ["1", "x"], ["2", "z"]])

    def test_malformed_patch_json_types(self):
        patch = load_patch("patch.json")
        patch["changes"][0]["row"] = "0"
        with self.assertRaises(PatchError) as ctx:
            apply_patch(read_fixture("source.csv"), patch)
        self.assertIs(ctx.exception.code, PatchErrorCode.INVALID_PATCH)

    def test_row_out_of_range(self):
        source = read_fixture("source.csv")
        patch = make_patch(source, [{"row": 3, "column": 0, "expected": "x", "value": "y"}])
        with self.assertRaises(PatchError) as ctx:
            apply_patch(source, patch)
        self.assertIs(ctx.exception.code, PatchErrorCode.ROW_OUT_OF_RANGE)

    def test_column_out_of_range(self):
        source = read_fixture("source.csv")
        patch = make_patch(source, [{"row": 0, "column": 4, "expected": "", "value": "x"}])
        with self.assertRaises(PatchError) as ctx:
            apply_patch(source, patch)
        self.assertIs(ctx.exception.code, PatchErrorCode.UNKNOWN_COLUMN)


class RendererTests(unittest.TestCase):
    def test_same_value_keeps_original_bytes(self):
        # A same-value replacement of a quoted field must not unquote it or
        # normalise escaping; the renderer preserves the original style.
        source = b'\xef\xbb\xbfa,"b""c",d\r\n1,"x",y\r\n'
        patch = make_patch(source, [{"row": 0, "column": 1, "expected": "x", "value": "x"}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], source)
        verify(source, result["output"], patch, result["map"])

    def test_same_value_unquoted_keeps_bytes(self):
        source = b"h1,h2\nv1,v2"
        patch = make_patch(source, [{"row": 0, "column": 0, "expected": "v1", "value": "v1"}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], source)
        self.assertEqual(result["map"]["target_size"], len(source))

    def test_quoted_same_value_with_special_chars(self):
        source = b'a,b\n"x,y","p""q"\n'
        patch = make_patch(
            source,
            [
                {"row": 0, "column": 0, "expected": "x,y", "value": "x,y"},
                {"row": 0, "column": 1, "expected": 'p"q', "value": 'p"q'},
            ],
        )
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], source)

    def test_unquoted_value_with_quote_upgrades(self):
        source = b"a,b\nx,y\n"
        patch = make_patch(source, [{"row": 0, "column": 0, "expected": "x", "value": 'q"z'}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], b'a,b\n"q""z",y\n')
        self.assertEqual(logical_rows(result["output"])[1][0], 'q"z')


class VariedReplacementTests(unittest.TestCase):
    def _roundtrip(self, source: bytes, changes: list[dict]):
        patch = make_patch(source, changes)
        result = apply_patch(source, patch)
        verify(source, result["output"], patch, result["map"])
        return result

    def test_varying_lengths_cross_line_quotes_and_cjk(self):
        source = (
            b'\xef\xbb\xbf"id","note"\r\n'
            b'1,"short"\n'
            b'2,"first\r\nsecond\r\nthird"\r\n'
            b'3,plain'
        )
        changes = [
            {"row": 0, "column": 1, "expected": "short", "value": "a" * 200},
            {"row": 1, "column": 1, "expected": "first\r\nsecond\r\nthird", "value": "中文字段，含逗号\n和换行"},
            {"row": 2, "column": 1, "expected": "plain", "value": "plain-but-longer"},
        ]
        result = self._roundtrip(source, changes)
        depatched = logical_rows(result["output"])
        self.assertEqual(depatched, [["id", "note"], ["1", "a" * 200], ["2", "中文字段，含逗号\n和换行"], ["3", "plain-but-longer"]])
        # Mixed terminators and no final newline are preserved.
        self.assertEqual(result["output"][-1:], b"r")
        self.assertIn(b"plain-but-longer", result["output"])
        self.assertNotIn(b"first", result["output"])

    def test_randomized_roundtrips(self):
        rng = random.Random(1234)
        pieces = ["plain", "中文", "a,b", 'q"r', "line\nbreak", "cr\r\nlf", ""]
        for _ in range(40):
            rows = []
            for _row in range(rng.randint(1, 4)):
                cells = []
                for _col in range(3):
                    value = rng.choice(pieces)
                    if value == "":
                        cells.append("")
                    elif any(ch in value for ch in ',"\r\n'):
                        cells.append('"' + value.replace('"', '""') + '"')
                    else:
                        cells.append(value)
                rows.append(",".join(cells))
            header = "c0,c1,c2"
            body = (header + "\r\n" + "\n".join(rows)).encode("utf-8")
            if rng.random() < 0.5:
                body += b"\r\n"
            changes = []
            for r in range(len(rows)):
                c = rng.randrange(3)
                old = logical_rows(body)[r + 1][c]
                new = rng.choice(pieces)
                changes.append(
                    {"row": r, "column": c, "expected": old, "value": new}
                )
            result = self._roundtrip(body, changes)
            self.assertEqual(logical_rows(result["output"]), logical_rows(body) if all(
                c["expected"] == c["value"] for c in changes
            ) else self._expected(body, changes))

    @staticmethod
    def _expected(source: bytes, changes: list[dict]) -> list[list[str]]:
        rows = logical_rows(source)
        for change in changes:
            rows[change["row"] + 1][change["column"]] = change["value"]
        return rows

    def test_header_can_be_quoted_and_is_untouched(self):
        source = b'\xef\xbb\xbf"weird header","x"\n"1","2"\n'
        result = self._roundtrip(source, [{"row": 0, "column": 0, "expected": "1", "value": "changed"}])
        self.assertTrue(result["output"].startswith(b'\xef\xbb\xbf"weird header","x"\n'))
        self.assertIn(b'"changed","2"\n', result["output"])

    def test_batch_rejects_one_bad_among_good(self):
        source = read_fixture("source.csv")
        patch = make_patch(
            source,
            [
                {"row": 0, "column": 1, "expected": "Alice", "value": "A"},
                {"row": 0, "column": 2, "expected": "WRONG", "value": "B"},
            ],
        )
        with self.assertRaises(PatchError) as ctx:
            apply_patch(source, patch)
        self.assertIs(ctx.exception.code, PatchErrorCode.WRONG_OLD_VALUE)


class EdgeCaseTests(unittest.TestCase):
    def test_empty_unquoted_field_insertion(self):
        source = b"a,b\nx,\n"
        patch = make_patch(source, [{"row": 0, "column": 1, "expected": "", "value": "tail"}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], b"a,b\nx,tail\n")
        verify(source, result["output"], patch, result["map"])

    def test_empty_quoted_field_stays_quoted_on_insertion(self):
        source = b'a,b\nx,""\n'
        patch = make_patch(source, [{"row": 0, "column": 1, "expected": "", "value": "tail"}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], b'a,b\nx,"tail"\n')
        verify(source, result["output"], patch, result["map"])

    def test_empty_quoted_field_same_value_keeps_lexeme(self):
        source = b'a,b\nx,""\n'
        patch = make_patch(source, [{"row": 0, "column": 1, "expected": "", "value": ""}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], source)

    def test_no_bom_crlf_only_no_final_newline(self):
        source = b"a,b\r\nx,\xc2\xa0y\r\n"
        patch = make_patch(source, [{"row": 0, "column": 1, "expected": "\xa0y", "value": "z"}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], b"a,b\r\nx,z\r\n")
        self.assertNotIn(b"\xef\xbb\xbf", result["output"])

    def test_trailing_comma_yields_empty_last_field(self):
        source = b"a,b,c\n1,2,\n"
        doc = scan_document(source)
        self.assertEqual(len(doc.records[0].cells), 3)
        self.assertEqual(doc.records[0].cells[2].value, "")
        self.assertEqual(doc.records[0].cells[2].start, doc.records[0].cells[2].end)
        patch = make_patch(source, [{"row": 0, "column": 2, "expected": "", "value": "3"}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], b"a,b,c\n1,2,3\n")

    def test_quoted_field_at_eof_without_newline(self):
        source = b'a,b\n1,"last, field"'
        patch = make_patch(source, [{"row": 0, "column": 1, "expected": "last, field", "value": 'x"y'}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], b'a,b\n1,"x""y"')
        verify(source, result["output"], patch, result["map"])

    def test_header_only_with_empty_patch_is_a_pure_copy(self):
        source = b'\xef\xbb\xbf"a",b\r\n'
        patch = make_patch(source, [])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], source)
        self.assertEqual(len(result["map"]["segments"]), 1)
        self.assertEqual(result["map"]["segments"][0]["kind"], "copy")
        verify(source, result["output"], patch, result["map"])

    def test_lone_cr_inside_quotes_is_data(self):
        source = b'a,b\n1,"x\ry"\n'
        doc = scan_document(source)
        self.assertEqual(doc.records[0].cells[1].value, "x\ry")
        patch = make_patch(source, [{"row": 0, "column": 1, "expected": "x\ry", "value": "z"}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], b'a,b\n1,"z"\n')

    def test_single_column_empty_value_uses_quotes_to_stay_a_record(self):
        # An unquoted empty single-column lexeme would be a blank line, i.e. a
        # zero-field record.  The renderer must keep one empty field.
        source = b"h\nx\ny\n"
        patch = make_patch(source, [{"row": 0, "column": 0, "expected": "x", "value": ""}])
        result = apply_patch(source, patch)
        self.assertEqual(result["output"], b'h\n""\ny\n')
        self.assertEqual(logical_rows(result["output"]), [["h"], [""], ["y"]])
        verify(source, result["output"], patch, result["map"])

    def test_verify_rejects_wrong_map_replacement_lexeme(self):
        source = b"a,b\nx,y\n"
        patch = make_patch(source, [{"row": 0, "column": 0, "expected": "x", "value": "q"}])
        result = apply_patch(source, patch)
        bad_map = json.loads(json.dumps(result["map"]))
        replacement = next(s for s in bad_map["segments"] if s["kind"] == "replace")
        # Claim a different new value; the lexeme no longer matches.
        replacement["new"] = "different"
        with self.assertRaises(PatchError):
            verify(source, result["output"], patch, bad_map)


class MapTests(unittest.TestCase):
    def setUp(self):
        self.source = read_fixture("source.csv")
        self.patch = load_patch("patch.json")
        self.result = apply_patch(self.source, self.patch)

    def test_segments_tile_both_files(self):
        segments = self.result["map"]["segments"]
        source_pos = target_pos = 0
        for segment in segments:
            self.assertEqual(segment["source_start"], source_pos)
            self.assertEqual(segment["target_start"], target_pos)
            source_pos = segment["source_end"]
            target_pos = segment["target_end"]
        self.assertEqual(source_pos, len(self.source))
        self.assertEqual(target_pos, len(self.result["output"]))

    def test_copy_segments_identical_and_replace_has_cell(self):
        segments = self.result["map"]["segments"]
        for segment in segments:
            s = slice(segment["source_start"], segment["source_end"])
            t = slice(segment["target_start"], segment["target_end"])
            if segment["kind"] == "copy":
                self.assertEqual(self.source[s], self.result["output"][t])
            else:
                self.assertIn("cell", segment)
                self.assertIn("old", segment)
                self.assertIn("new", segment)
                self.assertEqual(segment["new"], self.patch["changes"][
                    [ (c["row"], c["column"]) for c in self.patch["changes"] ].index(
                        (segment["cell"]["row"], segment["cell"]["column"])
                    )
                ]["value"])

    def test_verifier_detects_tampering(self):
        target = self.result["output"]
        result_map = self.result["map"]
        # Flip a copied byte outside any replacement.
        tampered = bytearray(target)
        tampered[10] ^= 0x20
        with self.assertRaises(PatchError):
            verify(self.source, bytes(tampered), self.patch, result_map)

        # Change a replacement to the wrong logical value.
        segments = result_map["segments"]
        replacement = next(s for s in segments if s["kind"] == "replace")
        bad = bytearray(target)
        bad[replacement["target_start"]] = ord("Z")
        with self.assertRaises(PatchError):
            verify(self.source, bytes(bad), self.patch, result_map)

    def test_verifier_detects_bom_or_terminator_drift(self):
        self.assertFalse(self.result["output"].endswith(b"\n"))
        with self.assertRaises(PatchError):
            verify(self.source, self.result["output"] + b"\n", self.patch, self.result["map"])
        with self.assertRaises(PatchError):
            verify(self.source, b"X" + self.result["output"][3:], self.patch, self.result["map"])


class CliTests(unittest.TestCase):
    def run_cli(self, patch_name: str, tmpdir: str):
        corrected = os.path.join(tmpdir, "corrected.csv")
        map_path = os.path.join(tmpdir, "map.json")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "csv_fidelity",
                "--source",
                SOURCE,
                "--patch",
                os.path.join(FIXTURES, patch_name),
                "--corrected",
                corrected,
                "--map",
                map_path,
            ],
            cwd=os.path.dirname(FIXTURES),
            capture_output=True,
            text=True,
        )
        return proc, corrected, map_path

    def test_success_writes_outputs_and_source_untouched(self):
        before = read_fixture("source.csv")
        with tempfile.TemporaryDirectory() as tmpdir:
            proc, corrected, map_path = self.run_cli("patch.json", tmpdir)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(os.path.exists(corrected))
            self.assertTrue(os.path.exists(map_path))
            with open(map_path, "r", encoding="utf-8") as handle:
                result_map = json.load(handle)
            with open(corrected, "rb") as handle:
                corrected_bytes = handle.read()
            verify(before, corrected_bytes, load_patch("patch.json"), result_map)
        self.assertEqual(read_fixture("source.csv"), before)

    def test_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proc, corrected, map_path = self.run_cli("wrong-old-value.json", tmpdir)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("WRONG_OLD_VALUE", proc.stderr)
            self.assertFalse(os.path.exists(corrected))
            self.assertFalse(os.path.exists(map_path))

    def test_duplicate_failure_publishes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            proc, corrected, map_path = self.run_cli("duplicate-cell.json", tmpdir)
            self.assertEqual(proc.returncode, 2)
            self.assertIn("DUPLICATE_CELL", proc.stderr)
            self.assertFalse(os.path.exists(corrected))
            self.assertFalse(os.path.exists(map_path))

    def test_refuses_to_overwrite_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Copy the source so the fixture is not at risk, then target it.
            source = os.path.join(tmpdir, "source.csv")
            with open(SOURCE, "rb") as handle:
                data = handle.read()
            with open(source, "wb") as handle:
                handle.write(data)
            patch_path = os.path.join(tmpdir, "patch.json")
            with open(patch_path, "w", encoding="utf-8") as handle:
                json.dump(load_patch("patch.json"), handle)
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "csv_fidelity",
                    "--source",
                    source,
                    "--patch",
                    patch_path,
                    "--corrected",
                    source,
                    "--map",
                    os.path.join(tmpdir, "map.json"),
                ],
                cwd=os.path.dirname(FIXTURES),
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("OUTPUT_IS_SOURCE", proc.stderr)
            with open(source, "rb") as handle:
                self.assertEqual(handle.read(), data)


class CsvLibraryCrossCheckTests(unittest.TestCase):
    def test_reader_equals_scanner_values(self):
        for name in ("source.csv",):
            data = read_fixture(name)
            doc = scan_document(data)
            rows = logical_rows(data)
            self.assertEqual(rows[0], doc.header)
            for record, row in zip(doc.records, rows[1:]):
                self.assertEqual([c.value for c in record.cells], row)

    def test_reader_agrees_on_varied_dialect(self):
        data = b'\xef\xbb\xbf"a,b","c""d"\r\n"x\r\ny",z\n'
        doc = scan_document(data)
        rows = logical_rows(data)
        self.assertEqual(rows, [["a,b", 'c"d'], ["x\r\ny", "z"]])
        self.assertEqual(doc.header, rows[0])
        self.assertEqual([c.value for c in doc.records[0].cells], rows[1])


if __name__ == "__main__":
    unittest.main()
