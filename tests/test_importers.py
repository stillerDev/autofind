"""Seed imports: Everything EFU and Listary text lists become indexed names."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.importers import (
    Entry,
    detect_format,
    filetime_to_unix,
    read_efu,
    read_list,
    read_listary,
    seed_index,
)
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.store import Store
from core.config import IndexConfig

EFU = (
    "Filename,Size,Date Modified,Date Created,Attributes\n"
    "C:\\work\\report.docx,15234,133012345670000000,133012345670000000,32\n"
    "C:\\work\\shot.png,88123,133000000000000000,133000000000000000,32\n"
    "broken,row\n"
    "C:\\work\\plan.txt,100,not-a-date,0,0\n"
)

LISTARY = "\n".join([
    '"C:\\Users\\me\\notes.md"',
    "C:\\Users\\me\\cv.pdf",
    "not a path at all",
    "",
    "D:\\data\\данные.xlsx",
])


class EfuTests(unittest.TestCase):
    def test_filetime_becomes_unix_time(self):
        # the FILETIME epoch itself: 1601-01-01 must map to Unix 0
        # (11 644 473 600 s x 10^7 ticks = 116 444 736 000 000 000)
        self.assertAlmostEqual(filetime_to_unix(116_444_736_000_000_000), 0.0, delta=1e-6)

    def test_efu_rows_and_bad_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "export.efu"
            path.write_text(EFU, encoding="utf-8-sig")
            self.assertEqual(detect_format(path), "efu")
            entries = read_efu(path)
            self.assertEqual(len(entries), 4)  # the broken row is skipped
            by_name = {Path(e.path).name: e for e in entries}
            self.assertEqual(by_name["report.docx"].size, 15234)
            self.assertGreater(by_name["report.docx"].mtime, 1_000_000_000)
            self.assertEqual(by_name["plan.txt"].mtime, 0.0)  # bad date tolerated


class ListaryTests(unittest.TestCase):
    def test_lines_that_look_like_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "listary.txt"
            path.write_text(LISTARY, encoding="utf-8")
            self.assertEqual(detect_format(path), "listary")
            entries = read_listary(path)
            names = {Path(e.path).name for e in entries}
            self.assertEqual(names, {"notes.md", "cv.pdf", "данные.xlsx"})

    def test_autodetect_picks_the_reader(self):
        with tempfile.TemporaryDirectory() as tmp:
            efu = Path(tmp) / "a.efu"
            efu.write_text(EFU, encoding="utf-8")
            txt = Path(tmp) / "b.txt"
            txt.write_text(LISTARY, encoding="utf-8")
            self.assertEqual(len(read_list(efu)), 4)
            self.assertEqual(len(read_list(txt)), 3)


class SeedTests(unittest.TestCase):
    def test_seed_makes_names_searchable_without_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            embedder = HashingEmbedder()
            store = Store(db, embedder.name, embedder.dim)
            entries = [
                Entry(path="C:\\work\\отчёт-2026.docx", size=15234, mtime=1_740_000_000),
                Entry(path="C:\\work\\отчёт-2025.docx", size=14300, mtime=1_700_000_000),
                Entry(path="C:\\work\\фото.png", size=88123, mtime=1_700_000_000),
            ]
            result = seed_index(store, entries)
            self.assertEqual(result["added"], 3)
            # a second pass skips what is already indexed, never downgrading it
            again = seed_index(store, entries)
            self.assertEqual(again["added"], 0)
            self.assertEqual(again["skipped"], 3)
            row = store.get_file("C:\\work\\отчёт-2026.docx")
            self.assertIsNotNone(row)
            self.assertEqual(row["kind"], "docx")
            self.assertEqual(row["sha256"], "")
            store.close()

    def test_seeded_rows_survive_a_real_index_run(self):
        """The seed is a placeholder; the next normal run keeps and enriches."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "report.docx").write_bytes(b"stub")  # replaced below
            embedder = HashingEmbedder()
            store = Store(db, embedder.name, embedder.dim)
            seed_index(store, [Entry(path=str(root / "report.docx"),
                                     size=4, mtime=root.joinpath("report.docx").stat().st_mtime)])
            store.close()
            from tests.test_core import make_docx
            make_docx(root / "report.docx", ["Инсталлятор не должен превышать 150 МБ."])
            store = Store(db, embedder.name, embedder.dim)
            try:
                stats = run_index([root], IndexConfig(home=Path(tmp) / "home"),
                                  store, embedder)
                self.assertGreaterEqual(stats.indexed, 1)
                row = store.get_file(str(root / "report.docx"))
                self.assertNotEqual(row["sha256"], "")  # now it has real content
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
