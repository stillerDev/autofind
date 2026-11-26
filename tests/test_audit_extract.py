"""Regressions for the audit findings in extract / embed / bm25 / chunker / indexer.

Each test pins one repaired behaviour: a bounded read, a corrected codepage, a
currency of stats on a failed worker, or a span whose offsets match its text.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from xml.etree import ElementTree as ET

from core.bm25 import _SegLRU
from core.chunker import chunk_spans
from core.config import IndexConfig
from core.embed.hashing import DIM, HashingEmbedder
from core.extract import EXTRACTORS, extract_text
from core.extract.dates import find_doc_date
from core.extract.office import extract_epub, extract_rtf, extract_xlsx
from core.extract.pdf import extract_pdf
from core.extract.plain import extract_plain_bytes
from core.indexer import _extract_image_job, run_index
from core.store import Store

S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class DateTests(unittest.TestCase):
    def test_two_digit_years_use_the_current_century_pivot(self):
        self.assertEqual(find_doc_date("05.03.95"), "1995-03-05")
        self.assertEqual(find_doc_date("05.03.05"), "2005-03-05")

    def test_impossible_calendar_dates_are_rejected(self):
        self.assertIsNone(find_doc_date("31.02.2026"))
        self.assertEqual(find_doc_date("28.02.2026"), "2026-02-28")


class PlainTests(unittest.TestCase):
    def test_binary_magic_is_not_indexed_as_text(self):
        self.assertEqual(extract_plain_bytes(b"%PDF-1.7\nhello"), "")
        self.assertEqual(extract_plain_bytes(b"\xff\xd8\xff\xe0JFIF"), "")


class BudgetKindTests(unittest.TestCase):
    def test_msg_kind_has_a_budgeted_extractor(self):
        self.assertIn("msg", EXTRACTORS)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.msg"
            path.write_bytes(b"stub")
            with mock.patch(
                "core.extract.extract_msgoutlook", return_value="Subject: hi"
            ) as spy:
                self.assertEqual(extract_text(path, "msg"), "Subject: hi")
        spy.assert_called_once()


class EmbedderTests(unittest.TestCase):
    def test_empty_generator_does_not_reach_vstack(self):
        self.assertEqual(
            HashingEmbedder().encode_passages(iter(())).shape, (0, DIM)
        )


class SegmentCacheTests(unittest.TestCase):
    def test_single_oversized_segment_cannot_break_the_byte_cap(self):
        cache = _SegLRU(max_bytes=16)
        cache.put(1, b"x" * 4096)
        self.assertLessEqual(cache._bytes, 16)
        cache.put(2, b"y" * 4096)
        self.assertLessEqual(cache._bytes, 16)


class ChunkerTests(unittest.TestCase):
    def test_cjk_sentence_stops_split_a_chunk(self):
        text = "一二。 三四。 五六。 七八。"
        self.assertEqual(len(chunk_spans(text, 6, 0)), 4)


class OfficeTests(unittest.TestCase):
    def test_rtf_honours_ansicpg_for_hex_escapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.rtf"
            path.write_bytes(
                b"{\\rtf1\\ansi\\ansicpg1252 Hello \\'80 world}"
            )
            self.assertIn("\u20ac", extract_rtf(path))
            path.write_bytes(
                b"{\\rtf1\\ansi\\ansicpg1251 Hello \\'80 world}"
            )
            self.assertNotIn("\u20ac", extract_rtf(path))

    def test_negative_shared_string_index_reads_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.xlsx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr(
                    "xl/sharedStrings.xml",
                    f'<sst xmlns="{S}"><si><t>FIRST</t></si>'
                    "<si><t>LAST</t></si></sst>",
                )
                archive.writestr(
                    "xl/worksheets/sheet1.xml",
                    f'<worksheet xmlns="{S}"><sheetData><row r="1">'
                    '<c t="s"><v>-1</v></c><c t="s"><v>0</v></c>'
                    "</row></sheetData></worksheet>",
                )
            text = extract_xlsx(path)
        self.assertIn("FIRST", text)
        self.assertNotIn("LAST", text)

    def test_one_bad_epub_chapter_does_not_skip_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.epub"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("ch1.xhtml", "<html>bad</html>")
                archive.writestr("ch2.xhtml", "<html><p>alive chapter</p></html>")
            with mock.patch(
                "core.extract.office.bounded_read",
                side_effect=[ET.ParseError("bad chapter"), b"<p>alive chapter</p>"],
            ):
                text = extract_epub(path)
        self.assertIn("alive chapter", text)


class PdfTests(unittest.TestCase):
    def test_late_ocr_failure_keeps_the_text_already_read(self):
        class FakePage:
            def get_text(self, _mode):
                return "page one 42"

        class FakeDocument:
            def __len__(self):
                return 1

            def __iter__(self):
                return iter([FakePage()])

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        fake_pymupdf = SimpleNamespace(open=lambda _path: FakeDocument())
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.pdf"
            path.write_bytes(b"stub")
            with mock.patch.dict("sys.modules", {"pymupdf": fake_pymupdf}), mock.patch(
                "core.extract.pdf._ocr_scanned", side_effect=RuntimeError("late OCR error")
            ):
                text = extract_pdf(path, ocr=True)
        self.assertIn("page one 42", text)


class IndexerTests(unittest.TestCase):
    def test_failed_worker_is_charged_and_the_run_commits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            root.mkdir()
            for name in ("a.txt", "b.txt"):
                (root / name).write_text("a searchable line of text " * 4, encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            store = Store(Path(tmp) / "index.db", "hashing-384", DIM)
            embedder = HashingEmbedder()

            class FailingPool:
                def submit(self, _fn, *_args, **_kwargs):
                    future = Future()
                    future.set_exception(RuntimeError("worker exploded"))
                    return future

                def shutdown(self, **_kwargs):
                    pass

            commit = mock.Mock(wraps=store.commit)
            try:
                with mock.patch(
                    "core.indexer._open_pool", return_value=FailingPool()
                ), mock.patch.object(store, "commit", commit):
                    stats = run_index([root], config, store, embedder, workers=2)
                self.assertEqual(stats.failed, 2)
                self.assertGreaterEqual(commit.call_count, 1)
                self.assertTrue(
                    any("worker RuntimeError" in message for _, message in stats.errors)
                )
            finally:
                store.close()

    def test_twin_lookup_uses_the_job_config_db_path(self):
        entry = SimpleNamespace(path="fake.png", kind="image", size=1, mtime=1.0)
        with tempfile.TemporaryDirectory() as tmp:
            config = IndexConfig(home=Path(tmp) / "home")
            from PIL import Image

            with mock.patch(
                "core.indexer._worker_twin_image", return_value=None
            ) as twin, mock.patch(
                "core.indexer.load_image", return_value=Image.new("RGB", (2, 2))
            ), mock.patch(
                "core.indexer.sha256_file", return_value="abc"
            ), mock.patch(
                "core.indexer.dhash", return_value=0
            ), mock.patch(
                "core.indexer.lifespan.on_battery", return_value=True
            ), mock.patch(
                "core.indexer._worker_visual", side_effect=RuntimeError("stop here")
            ), self.assertRaises(RuntimeError):
                _extract_image_job(entry, config)
        twin.assert_called_once()
        self.assertEqual(twin.call_args[0][0], config.db_path)

    def test_sequential_progress_reaches_the_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "docs"
            root.mkdir()
            for name in ("a.txt", "b.txt"):
                (root / name).write_text("a searchable line of text " * 4, encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            store = Store(Path(tmp) / "index.db", "hashing-384", DIM)
            events = []
            try:
                run_index(
                    [root],
                    config,
                    store,
                    HashingEmbedder(),
                    workers=1,
                    progress=lambda done, total, _status, _path: events.append(
                        (done, total)
                    ),
                )
            finally:
                store.close()
        self.assertTrue(events)
        self.assertEqual(events[-1], (2, 2))


if __name__ == "__main__":
    unittest.main()
