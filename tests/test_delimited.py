"""CSV/TSV extraction shaped like `pandas.head`.

Ports of Kensaku `delimited.rs`'s unit tests: delimiter sniffing, the row
shape, the column summary, quoted commas, embedded newlines, truncation and
the binary rejection.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.extract import extract_text
from core.extract.delimited import (
    HEAD_ROWS,
    extract_delimited,
    _cell_kind,
    _sniff,
)


class SniffTests(unittest.TestCase):
    def test_comma_semicolon_and_tab_are_distinguished(self):
        self.assertEqual(_sniff("a,b,c\n1,2,3\n4,5,6\n", ","), ",")
        self.assertEqual(_sniff("a;b;c\n1;2;3\n4;5;6\n", ","), ";")
        self.assertEqual(_sniff("a\tb\tc\n1\t2\t3\n4\t5\t6\n", ","), "\t")

    def test_hint_wins_a_tie(self):
        # Single-column rows agree on everything; the hint must decide.
        self.assertEqual(_sniff("a\n1\n2\n", "\t"), "\t")
        self.assertEqual(_sniff("a\n1\n2\n", ","), ",")

    def test_quoted_commas_do_not_confuse_the_sniffer(self):
        text = 'name,note\n"-alpha","2,500 total"\n"beta","3,000"\n'
        self.assertEqual(_sniff(text, ","), ",")


class RowShapeTests(unittest.TestCase):
    def test_rows_become_column_value_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.csv"
            path.write_text(
                "name,plan\nanna,pro\noleg,team\n", encoding="utf-8"
            )
            text = extract_delimited(path)
        self.assertIn('columns: name (text), plan (text)', text)
        self.assertIn('name = "anna", plan = "pro"', text)
        self.assertIn('name = "oleg", plan = "team"', text)

    def test_types_are_inferred_from_the_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mixed.csv"
            path.write_text("n,price,who\n1,1.5,a\n2,2,b\n", encoding="utf-8")
            text = extract_delimited(path)
        self.assertIn("n (integer)", text)
        self.assertIn("price (number)", text)
        self.assertIn("who (text)", text)

    def test_one_float_turns_an_integer_column_into_a_number(self):
        self.assertEqual(_cell_kind("2", "integer"), "integer")
        self.assertEqual(_cell_kind("2.5", "integer"), "number")
        self.assertEqual(_cell_kind("2", "number"), "number")
        self.assertEqual(_cell_kind("x", "integer"), "text")
        self.assertEqual(_cell_kind("", "integer"), "integer")

    def test_quoted_field_with_comma_and_embedded_newline(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "quoted.csv"
            path.write_text(
                'who,note\n"smith","line one\nline two, still"\n',
                encoding="utf-8",
            )
            text = extract_delimited(path)
        self.assertIn('note = "line one line two, still"', text, "newline flattened")

    def test_header_only_file_indexes_its_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.csv"
            path.write_text("a,b,c\n", encoding="utf-8")
            self.assertEqual(extract_delimited(path), "a, b, c")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blank.csv"
            path.write_text("\n\n", encoding="utf-8")
            self.assertEqual(extract_delimited(path), "")

    def test_head_rows_cap_marks_the_summary(self):
        rows = "".join(f"{i}\n" for i in range(300))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "long.csv"
            path.write_text("n\n" + rows, encoding="utf-8")
            text = extract_delimited(path)
        self.assertIn("columns (first rows sampled):", text)
        self.assertEqual(len(text.splitlines()), HEAD_ROWS + 1)

    def test_byte_cap_keeps_whole_rows_and_drops_the_half_record(self):
        rows = "".join("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n" for _ in range(100))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wide.csv"
            path.write_text("n\n" + rows, encoding="utf-8")
            with mock.patch.object(
                __import__("core.extract.delimited", fromlist=["HEAD_ROWS"]),
                "HEAD_ROWS",
                10_000,
            ):
                text = extract_delimited(path, max_bytes=200)
        self.assertTrue(text.startswith("columns:"))
        self.assertNotIn("aa\n", text.splitlines()[-1] + "\n")
        for line in text.splitlines()[1:]:
            self.assertTrue(line.endswith('"'), f"whole row kept: {line!r}")

    def test_utf16_with_bom_decodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "unicode.csv"
            path.write_bytes("name\nанна\n".encode("utf-16"))
            text = extract_delimited(path)
        self.assertIn('name = "анна"', text)

    def test_binary_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "noisy.csv"
            path.write_bytes(b"a,b\n\x00\x01\x00\x02garbage\xff\xfe\n")
            self.assertEqual(extract_delimited(path), "")

    def test_through_the_dispatcher_as_text_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sheet.csv"
            path.write_text("city,pop\nmoscow,12500000\n", encoding="utf-8")
            text = extract_text(path, "text")
        self.assertIn('city = "moscow"', text)


if __name__ == "__main__":
    unittest.main()
