"""Zip-bomb budgets for the office extractors.

Ports of the ideas (and the constant values) behind Kensaku 1.0.17's
`office.rs` budgets: every archive member is read through a capped window and
XML work is charged to per-archive budgets. A bomb must come back as a normal
truncated document, never as a wedge or a crash — each test shrinks a constant
so the bomb is a few kilobytes on disk.
"""

from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from core.extract import extract_text
from core.extract import docx as docx_module
from core.extract import office
from core.extract.office import Budget, BudgetExhausted, _BoundedReader

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
T = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _writestr_deflated(archive: zipfile.ZipFile, name: str, blob: str | bytes) -> None:
    """A member whose declared size is far above its on-disk size: the shape
    every decompression bomb takes."""
    info = zipfile.ZipInfo(name)
    info.compress_type = zipfile.ZIP_DEFLATED
    archive.writestr(info, blob)


class BoundedReaderTests(unittest.TestCase):
    def test_read_stops_at_the_window_and_charges_the_budget(self):
        data = bytes(range(256)) * 100  # 25_600 bytes
        budget = Budget()
        reader = _BoundedReader(io.BytesIO(data), budget, 1000)
        self.assertEqual(len(reader.read(1 << 20)), 1000, "one read is capped")
        self.assertEqual(len(reader.read(1 << 20)), 0, "the window is spent")
        self.assertEqual(budget.bytes, 1000)

    def test_archive_budget_fires_when_bytes_exceed_it(self):
        data = b"x" * 5000
        budget = Budget()
        with mock.patch.object(office, "MAX_ARCHIVE_BYTES", 2000):
            reader = _BoundedReader(io.BytesIO(data), budget, 5000)
            with self.assertRaises(BudgetExhausted):
                reader.read(5000)
        # What was read before the raise stays charged, for honest accounting.
        self.assertGreaterEqual(budget.bytes, 2000)


class PptxBudgetTests(unittest.TestCase):
    def test_window_truncates_a_giant_slide(self):
        # A ~2 MB slide body compresses to a few KB on disk: the bomb shape.
        body = "".join(f"<a:p><a:r><a:t>word{i:05d} ok</a:t></a:r></a:p>" for i in range(40_000))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.pptx"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(
                    archive, "ppt/slides/slide1.xml",
                    f'<?xml version="1.0"?><sld xmlns:a="{A}"><a:txBody>{body}</a:txBody></sld>',
                )
            self.assertLess(path.stat().st_size, 120_000, "the bomb compresses")
            with mock.patch.object(office, "MAX_XML_INPUT_BYTES", 4096):
                text = extract_text(path, "pptx")
        self.assertTrue(text, "a truncated slide still yields its first words")
        self.assertIn("word00000", text)
        self.assertNotIn("word39999", text, "past the window the text stops")

    def test_a_dead_slide_does_not_kill_the_deck(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.pptx"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(archive, "ppt/slides/slide1.xml", "<sld xmlns:a=''>oops")
                _writestr_deflated(
                    archive, "ppt/slides/slide2.xml",
                    f'<sld xmlns:a="{A}"><a:txBody><a:p><a:r><a:t>alive</a:t></a:r></a:p></a:txBody></sld>',
                )
            text = extract_text(path, "pptx")
        self.assertIn("alive", text, "the readable slide survives")


class XlsxBudgetTests(unittest.TestCase):
    def test_event_budget_truncates_millions_of_rows(self):
        # 20k rows are ~60k XML events; a budget of 1000 must stop the sheet.
        rows = "".join(
            f'<row r="{i}"><c><v>1</v></c><c><v>2</v></c></row>' for i in range(20_000)
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(
                    archive, "xl/worksheets/sheet1.xml",
                    f'<worksheet xmlns="{S}"><sheetData>{rows}</sheetData></worksheet>',
                )
            self.assertLess(path.stat().st_size, 120_000)
            with mock.patch.object(office, "MAX_XML_EVENTS", 1000):
                text = extract_text(path, "xlsx")
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(0 < len(lines) < 500, f"the event budget bit: {len(lines)} lines")

    def test_shared_string_caps_drop_only_the_tail(self):
        rows = [["абв", "гд"], ["ежз", "ик"]]  # cyrillic: chars, not bytes
        shared = [cell for row in rows for cell in row]
        strings = "".join(f"<si><t>{cell}</t></si>" for cell in shared)
        sheet = "".join(
            f'<row r="{i}">'
            + "".join(f'<c t="s"><v>{shared.index(cell)}</v></c>' for cell in row)
            + "</row>"
            for i, row in enumerate(rows, start=1)
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capped.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(archive, "xl/sharedStrings.xml", f'<sst xmlns="{S}">{strings}</sst>')
                _writestr_deflated(
                    archive, "xl/worksheets/sheet1.xml",
                    f'<worksheet xmlns="{S}"><sheetData>{sheet}</sheetData></worksheet>',
                )
            # Two strings, 6 chars in total: the byte cap stops the third.
            with mock.patch.object(office, "MAX_SHARED_STRING_BYTES", 6):
                text = extract_text(path, "xlsx")
        self.assertIn("абв | гд", text)
        self.assertNotIn("ежз", text, "past the byte cap cells read empty")

    def test_shared_string_count_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "capped.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(
                    archive, "xl/sharedStrings.xml",
                    '<sst xmlns="%s">%s</sst>'
                    % (S, "".join(f"<si><t>s{i}</t></si>" for i in range(10))),
                )
                cells = "".join(
                    f'<row r="{i}"><c t="s"><v>{i}</v></c></row>' for i in range(10)
                )
                _writestr_deflated(
                    archive, "xl/worksheets/sheet1.xml",
                    f'<worksheet xmlns="{S}"><sheetData>{cells}</sheetData></worksheet>',
                )
            with mock.patch.object(office, "MAX_SHARED_STRINGS", 3):
                text = extract_text(path, "xlsx")
        self.assertIn("s0", text)
        self.assertIn("s2", text)
        self.assertNotIn("s5", text)


class EpubBudgetTests(unittest.TestCase):
    def test_window_truncates_a_giant_chapter(self):
        chapter = "".join(f"<p>sentence {i:05d} about nothing much</p>" for i in range(20_000))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.epub"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(archive, "OEBPS/ch1.xhtml", f"<html><body>{chapter}</body></html>")
            with mock.patch.object(office, "MAX_XML_INPUT_BYTES", 2048):
                text = extract_text(path, "epub")
        self.assertTrue(text)
        self.assertIn("sentence 00000", text)
        self.assertNotIn("sentence 19999", text)

    def test_archive_budget_ends_the_book_after_some_chapters(self):
        chapter = "<p>" + "word " * 500 + "</p>"  # ~2.5 KB each, x12 chapters
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.epub"
            with zipfile.ZipFile(path, "w") as archive:
                for index in range(1, 13):
                    _writestr_deflated(
                        archive, f"OEBPS/ch{index}.xhtml", f"<html><body>{chapter}</body></html>"
                    )
            # 30 KB of budget across the whole archive: some chapters in.
            with mock.patch.object(office, "MAX_ARCHIVE_BYTES", 30_000):
                text = extract_text(path, "epub")
        chapters = len(text.splitlines())
        self.assertTrue(0 < chapters < 12, f"archive budget stopped the book at {chapters}")


class OdtBudgetTests(unittest.TestCase):
    def test_window_truncates_giant_content(self):
        body = "".join(f"<text:p>строка {i:05d} текста</text:p>" for i in range(30_000))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.odt"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(
                    archive, "content.xml", f'<office xmlns:text="{T}">{body}</office>'
                )
            with mock.patch.object(office, "MAX_XML_INPUT_BYTES", 4096):
                text = extract_text(path, "odt")
        self.assertTrue(text)
        self.assertIn("строка 00000", text)
        self.assertNotIn("строка 29999", text)


class DocxBudgetTests(unittest.TestCase):
    @staticmethod
    def _document(paragraphs: int) -> str:
        body = "".join(
            f'<w:p><w:r><w:t>пункт {i:05d} документа</w:t></w:r></w:p>'
            for i in range(paragraphs)
        )
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
        )

    def test_window_truncates_a_giant_document(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.docx"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(archive, "word/document.xml", self._document(30_000))
            self.assertLess(path.stat().st_size, 120_000)
            with mock.patch.object(office, "MAX_XML_INPUT_BYTES", 4096):
                # The zip fallback directly: python-docx may be installed, and
                # the budgeted reader is what this test is about.
                text = docx_module._extract_via_zip(path)
        self.assertTrue(text)
        self.assertIn("пункт 00000", text)
        self.assertNotIn("пункт 29999", text)

    def test_event_budget_truncates_a_flood_of_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "flood.docx"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(archive, "word/document.xml", self._document(5_000))
            with mock.patch.object(office, "MAX_XML_EVENTS", 60):
                text = docx_module._extract_via_zip(path)
        paragraphs = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(0 < len(paragraphs) < 100, f"{len(paragraphs)} kept")

    def test_tables_and_paragraphs_still_read_in_body_order(self):
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:document xmlns:w="{W}"><w:body>'
            '<w:p><w:r><w:t>before</w:t></w:r></w:p>'
            "<w:tbl><w:tr><w:tc><w:p><w:r><w:t>left</w:t></w:r></w:p></w:tc>"
            "<w:tc><w:p><w:r><w:t>right</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"
            '<w:p><w:r><w:t>after</w:t></w:r></w:p>'
            "</w:body></w:document>"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "table.docx"
            with zipfile.ZipFile(path, "w") as archive:
                _writestr_deflated(archive, "word/document.xml", document)
            text = docx_module._extract_via_zip(path)
        self.assertEqual(text.splitlines(), ["before", "left | right", "after"])

    def test_line_cap_stops_a_python_docx_flood(self):
        # A fake python-docx module: the budget on the collection loop is the
        # subject, not the library.
        class FakeParagraph:
            def __init__(self, text: str) -> None:
                self.text = text

        fake = mock.MagicMock()
        fake.Document.return_value.paragraphs = [
            FakeParagraph(f"line {i:06d}") for i in range(500)
        ]
        fake.Document.return_value.tables = []
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "real.docx"
            path.write_bytes(b"stub")
            with mock.patch.dict("sys.modules", {"docx": fake}), \
                 mock.patch.object(docx_module, "MAX_DOCX_LINES", 100):
                text = extract_text(path, "docx")
        self.assertEqual(len(text.splitlines()), 100)
        self.assertIn("line 000000", text)
        self.assertNotIn("line 000499", text)


if __name__ == "__main__":
    unittest.main()
