"""CLI 行为：正常输出 JSON，坏书以非零退出码拒绝且不产出成功语料文件。"""

from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from epub_extract.__main__ import main

BOOK = "fixtures/book.epub"
BAD_SPINE = "fixtures/bad-spine.epub"


class CliTests(unittest.TestCase):
    def test_book_outputs_json_to_stdout(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = main([BOOK])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(len(payload["body"]), 6)
        self.assertFalse(payload["complete"])

    def test_output_file_written(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "corpus.json"
            code = main([BOOK, "-o", str(out)])
            self.assertEqual(code, 0)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["summary"]["identifier"], "synthetic-book")

    def test_bad_spine_exits_nonzero_and_writes_nothing(self):
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "should-not-exist.json"
            err = io.StringIO()
            with redirect_stderr(err):
                code = main([BAD_SPINE, "-o", str(out)])
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())  # 未知 spine：不发布成功语料
            self.assertIn("idref", err.getvalue())


if __name__ == "__main__":
    unittest.main()
