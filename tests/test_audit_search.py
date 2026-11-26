"""Regression tests for the audit fixes in the search/index core files."""

from __future__ import annotations

import hashlib
import io
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from unittest import mock

import numpy as np
from PIL import Image

import core.ignore as ignore
import core.images as images
import core.nameindex as nameindex
import core.store as store_mod
import core.vectors as vecstore
from core import group, query, search
from core.query import parse


class NegationTests(unittest.TestCase):
    def test_negated_type_excludes_instead_of_selecting(self):
        parsed = parse("-type:pdf report")
        self.assertEqual(parsed.kinds, set())
        self.assertEqual(parsed.not_kinds, {"pdf"})
        self.assertEqual(parsed.text, "report")
        self.assertTrue(parsed.has_filters)
        self.assertFalse(parsed.matches(r"C:\docs\a.pdf", "pdf", 100, 0.0))
        self.assertTrue(parsed.matches(r"C:\docs\a.txt", "text", 100, 0.0))

    def test_negated_ext_and_lang_exclude_by_extension(self):
        self.assertEqual(parse("-ext:rs").not_exts, {".rs"})
        self.assertEqual(parse("-lang:python").not_exts, {".py", ".pyi"})
        parsed = parse("-ext:rs")
        self.assertFalse(parsed.matches(r"C:\src\main.rs", "code", 100, 0.0))
        self.assertTrue(parsed.matches(r"C:\src\main.py", "code", 100, 0.0))

    def test_negated_regex_is_an_exclusion(self):
        parsed = parse('-re:"foo" bar')
        self.assertEqual(parsed.regexes, [])
        self.assertEqual(parsed.not_regexes, ["foo"])
        self.assertFalse(parsed.matches(r"C:\docs\foo.txt", "text", 100, 0.0))
        self.assertTrue(parsed.matches(r"C:\docs\bar.txt", "text", 100, 0.0))
        self.assertEqual(parsed.chips[0].kind, "exclude")

    def test_negated_in_and_name(self):
        parsed = parse(r'-in:"C:\docs" -name:tmp report')
        self.assertFalse(parsed.matches(r"C:\docs\report.txt", "text", 100, 0.0))
        self.assertFalse(parsed.matches(r"C:\other\tmp.txt", "text", 100, 0.0))
        self.assertTrue(parsed.matches(r"C:\other\report.txt", "text", 100, 0.0))

    def test_one_time_rule_per_query(self):
        _, chips = query.compile_intent("за неделю за месяц")
        times = [chip for chip in chips if chip.kind in {"week", "month", "year"}]
        self.assertEqual(len(times), 1)


class ScopeTests(unittest.TestCase):
    def test_collections_are_a_union(self):
        class FakeStore:
            def collection_ids_for_names(self, names):
                return {
                    "Work": {1, 2}, "Home": {2, 3},
                }.get(tuple(names), set()) if len(names) == 1 else {1, 2, 3}

        parsed = SimpleNamespace(
            tags=[], collections=["Work", "Home"], ocr_terms=[],
            email_from=[], email_subject=[],
        )
        self.assertEqual(search._tag_collection_scope(FakeStore(), parsed), {1, 2, 3})

    def test_collection_union_still_intersects_tags(self):
        class FakeStore:
            def file_ids_for_tags(self, tags):
                return {2, 3, 4}

            def collection_ids_for_names(self, names):
                return {1, 2, 3}

        parsed = SimpleNamespace(
            tags=["invoice"], collections=["Work", "Home"], ocr_terms=[],
            email_from=[], email_subject=[],
        )
        self.assertEqual(search._tag_collection_scope(FakeStore(), parsed), {2, 3})


class NameIndexTests(unittest.TestCase):
    def setUp(self):
        rows = [
            {
                "id": 1, "path": r"C:\docs\readme.txt", "name": "readme.txt",
                "char_mask": 0, "kind": "text", "size": 10, "mtime": 1.0,
            },
            {
                "id": 2, "path": r"C:\other\a.md", "name": "a.md",
                "char_mask": 0, "kind": "text", "size": 20, "mtime": 2.0,
            },
        ]
        self.index = nameindex.NameIndex.from_rows(rows)

    def test_offsets_match_the_joined_blob(self):
        for offsets, blob in (
            (self.index.offsets, self.index.blob),
            (self.index.name_offsets, self.index.name_blob),
            (self.index.path_offsets, self.index.path_blob),
            (self.index.path_lower_offsets, self.index.path_lower_blob),
        ):
            self.assertEqual(int(offsets[-1]), len(blob))
        self.assertEqual(self.index.name(0), "readme.txt")
        self.assertEqual(self.index.name(1), "a.md")

    def test_long_prefix_and_scope_do_not_read_past_the_blob(self):
        self.assertEqual(self.index.prefix("a" * 500), [])
        # scope_positions expects already-lowered prefixes (search.py lowers them).
        positions = self.index.scope_positions([r"c:\docs", "z" * 500])
        self.assertEqual(positions.tolist(), [0])


class VectorTests(unittest.TestCase):
    def test_sketch_query_pad_for_non_multiple_of_64_dim(self):
        dim = 100  # 13 packed bytes: np.view(uint64) needs padding
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE vectors (id INTEGER PRIMARY KEY, owner_type TEXT, "
                "owner_id INTEGER, dim INTEGER, vec BLOB)"
            )
            rng = np.random.default_rng(3)
            rows = rng.standard_normal((300, dim), dtype=np.float32)
            rows /= np.linalg.norm(rows, axis=1, keepdims=True)
            conn.executemany(
                "INSERT INTO vectors(id, owner_type, owner_id, dim, vec) "
                "VALUES(?, 'chunk', ?, ?, ?)",
                [(i + 1, i + 1, dim, rows[i].tobytes()) for i in range(len(rows))],
            )
            conn.commit()
            try:
                pack = vecstore.build_pack(conn, db, dim)
            finally:
                conn.close()
            try:
                self.assertEqual(len(pack._search_sketch(rows[0], 3)), 3)
            finally:
                pack.close()

    def test_sketch_counters_are_thread_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            conn.execute(
                "CREATE TABLE vectors (id INTEGER PRIMARY KEY, owner_type TEXT, "
                "owner_id INTEGER, dim INTEGER, vec BLOB)"
            )
            rng = np.random.default_rng(4)
            rows = rng.standard_normal((20, 384), dtype=np.float32)
            conn.executemany(
                "INSERT INTO vectors(id, owner_type, owner_id, dim, vec) "
                "VALUES(?, 'chunk', ?, ?, ?)",
                [(i + 1, i + 1, 384, rows[i].tobytes()) for i in range(len(rows))],
            )
            conn.commit()
            try:
                pack = vecstore.build_pack(conn, db, 384)
            finally:
                conn.close()
            try:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    list(pool.map(lambda _: pack._note_sketch(True), range(400)))
                self.assertEqual(pack._sketch_tries, 400)
                self.assertEqual(pack._sketch_accepts, 400)
            finally:
                pack.close()


class StoreCacheTests(unittest.TestCase):
    def test_entry_eviction_still_applies_the_byte_budget(self):
        saved = dict(store_mod._DATA_CACHE)
        try:
            store_mod._DATA_CACHE.clear()
            with mock.patch.object(store_mod, "_DATA_CACHE_MAX_ENTRIES", 2), \
                    mock.patch.object(store_mod, "_DATA_CACHE_MAX_BYTES", 4):
                for key in ("a", "b", "c"):
                    store_mod._data_cache_put(key, b"12")
                self.assertEqual(len(store_mod._DATA_CACHE), 2)
                self.assertLessEqual(sum(len(v) for v in store_mod._DATA_CACHE.values()), 4)
        finally:
            store_mod._DATA_CACHE.clear()
            store_mod._DATA_CACHE.update(saved)

    def test_ram_bm25_enabled_by_default(self):
        self.assertTrue(store_mod._RAM_BM25_ENABLED)


class MiscCoreTests(unittest.TestCase):
    def test_ignore_regex_respects_case_insensitivity(self):
        rule = ignore.parse("Build/", case_insensitive=True)[0]
        self.assertTrue(rule.matches("Build", True))
        self.assertTrue(rule.matches("build", True))
        self.assertFalse(ignore.parse("build/")[0].matches("BUILD", True))

    def test_copy_group_registers_absorbed_hits(self):
        def hit(file_id, name):
            return SimpleNamespace(
                file_id=file_id, name=name, path=r"C:\x\\" + name, kind="text",
                phash=0, mtime=0.0, snippet="", width=0, height=0,
            )

        hashes = {1: b"h", 2: b"h", 3: b"h"}
        hits = [hit(1, "report.txt"), hit(2, "report (1).txt"), hit(3, "report (1).txt")]
        grouped = group.group_hits(hits, hashes=hashes)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0].size, 3)

    def test_image_digest_annotations_and_thumbnail_config(self):
        self.assertEqual(images.sha256_bytes(b"x"), hashlib.sha256(b"x").digest())
        self.assertIs(get_type_hints(images.ImageInfo)["sha256"], bytes)
        self.assertEqual(images.THUMB_SIZE, images.IndexConfig().thumb_size)

        class SmallConfig:
            thumb_size = 48

        image = Image.new("RGB", (200, 200), "white")
        with mock.patch.object(images, "IndexConfig", SmallConfig):
            thumb = Image.open(io.BytesIO(images.thumbnail_bytes(image)))
            self.assertLessEqual(max(thumb.size), 48)

    def test_walk_indexes_dotfiles_and_sniffs_unknown_extensions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env").write_text("SECRET_KEY=abcdefghijklmnop\n" * 2, encoding="utf-8")
            (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
            (root / "notes.unknownext").write_text(
                "plain text with enough characters to sniff\n", encoding="utf-8"
            )
            (root / "app.ts").write_text(
                "export const x = 1; // long enough to clear the size floor\n",
                encoding="utf-8",
            )
            from core.scanner import iter_entries

            entries = {
                Path(entry.path).name: entry.kind
                for entry in iter_entries([root], privacy=False, respect_ignore=False)
            }
            self.assertIn(".env", entries)
            self.assertNotIn(".gitignore", entries)
            self.assertEqual(entries["notes.unknownext"], "text")
            self.assertEqual(entries["app.ts"], "code")


if __name__ == "__main__":
    unittest.main()
