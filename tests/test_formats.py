"""New extractors and ignore-file parity."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from core.extract import extract_text
from core.ignore import IgnoreSet, parse
from core.scanner import iter_entries

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
T = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"


def make_pptx(path: Path, slides: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for index, text in enumerate(slides, start=1):
            body = "".join(f"<a:p><a:r><a:t>{line}</a:t></a:r></a:p>" for line in text.split("|"))
            archive.writestr(
                f"ppt/slides/slide{index}.xml",
                f'<?xml version="1.0"?><sld xmlns:a="{A}"><a:txBody>{body}</a:txBody></sld>',
            )


def make_xlsx(path: Path, rows: list[list[str]]) -> None:
    shared = [cell for row in rows for cell in row]
    strings = "".join(f"<si><t>{cell}</t></si>" for cell in shared)
    cells = ""
    for row_index, row in enumerate(rows, start=1):
        items = "".join(
            f'<c t="s"><v>{shared.index(cell)}</v></c>' for cell in row
        )
        cells += f'<row r="{row_index}">{items}</row>'
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{S}">{strings}</sst>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{S}"><sheetData>{cells}</sheetData></worksheet>')


def make_epub(path: Path, chapters: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for index, text in enumerate(chapters, start=1):
            archive.writestr(
                f"OEBPS/ch{index}.xhtml",
                f"<html><body><p>{text}</p></body></html>",
            )


def make_odt(path: Path, paragraphs: list[str]) -> None:
    body = "".join(f"<text:p>{text}</text:p>" for text in paragraphs)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("content.xml", f'<office xmlns:text="{T}">{body}</office>')


def make_rtf(path: Path, text: str) -> None:
    # Real RTF escapes non-ASCII as `\'hh` bytes in the document codepage, which
    # is exactly what the extractor has to undo.
    escaped = "".join(f"\\'{byte:02x}" for byte in text.encode("cp1251"))
    path.write_bytes(
        rb"{\rtf1\ansi\ansicpg1251\deff0{\fonttbl{\f0 Arial;}}{\pard " + escaped.encode() + rb"}}"
    )


class ExtractorTests(unittest.TestCase):
    def test_pptx_slides_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "deck.pptx"
            make_pptx(path, ["Первый|слайд", "Второй|слайд"])
            text = extract_text(path, "pptx")
            self.assertIn("Первый", text)
            self.assertLess(text.index("Первый"), text.index("Второй"))

    def test_xlsx_cells_per_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.xlsx"
            make_xlsx(path, [["факт", "план"], ["120", "150"]])
            text = extract_text(path, "xlsx")
            self.assertIn("факт | план", text)
            self.assertIn("120 | 150", text)

    def test_epub_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "book.epub"
            make_epub(path, ["Глава первая", "Глава вторая"])
            text = extract_text(path, "epub")
            self.assertIn("Глава первая", text)
            self.assertIn("Глава вторая", text)

    def test_odt_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.odt"
            make_odt(path, ["Первый абзац", "Второй абзац"])
            text = extract_text(path, "odt")
            self.assertEqual(text.splitlines(), ["Первый абзац", "Второй абзац"])

    def test_rtf_strips_control_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.rtf"
            make_rtf(path, "Смета на ремонт")
            text = extract_text(path, "rtf")
            self.assertIn("Смета", text)
            self.assertNotIn("\\rtf", text)

    def test_broken_package_is_empty_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.pptx"
            path.write_bytes(b"not a zip at all")
            self.assertEqual(extract_text(path, "pptx"), "")


class IgnoreTests(unittest.TestCase):
    def test_parse_skips_comments_and_blanks(self):
        rules = parse("# comment\n\nnode_modules/\n!keep.txt\n")
        self.assertEqual(len(rules), 2)
        self.assertTrue(rules[0].dir_only)
        self.assertTrue(rules[1].negated)

    def test_directory_only_does_not_match_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("build/\n", encoding="utf-8")
            ignore = IgnoreSet()
            ignore.add_directory(root)
            self.assertTrue(ignore.is_ignored(str(root / "build"), True))
            self.assertFalse(ignore.is_ignored(str(root / "build"), False))

    def test_negation_reincludes_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("*.log\n!keep.log\n", encoding="utf-8")
            ignore = IgnoreSet()
            ignore.add_directory(root)
            self.assertTrue(ignore.is_ignored(str(root / "debug.log"), False))
            self.assertFalse(ignore.is_ignored(str(root / "keep.log"), False))

    def test_anchored_pattern_matches_from_the_declaring_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".gitignore").write_text("/dist\n", encoding="utf-8")
            ignore = IgnoreSet()
            ignore.add_directory(root)
            self.assertTrue(ignore.is_ignored(str(root / "dist"), True))
            self.assertFalse(ignore.is_ignored(str(root / "src" / "dist"), True))

    def test_scanner_honours_gitignore(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / ".gitignore").write_text("ignored.txt\nsecret/\n", encoding="utf-8")
            (root / "kept.txt").write_text("нужный текст, достаточно длинный для индекса", encoding="utf-8")
            (root / "ignored.txt").write_text("этот файл игнорируется гитом и не должен попасть", encoding="utf-8")
            (root / "secret").mkdir()
            (root / "secret" / "hidden.txt").write_text("и это тоже не должно попасть в индекс", encoding="utf-8")

            names = {Path(entry.path).name for entry in iter_entries([root])}
            self.assertIn("kept.txt", names)
            self.assertNotIn("ignored.txt", names)
            self.assertNotIn("hidden.txt", names)

            everything = {Path(entry.path).name for entry in iter_entries([root], respect_ignore=False)}
            self.assertIn("ignored.txt", everything)


if __name__ == "__main__":
    unittest.main()
