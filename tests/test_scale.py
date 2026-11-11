"""Name index, vector pack and grouping — the three parts that decide whether
the app still feels instant on a corpus far bigger than this laptop's."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from core import filevec, nameindex, vectors
from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.group import dhash, group_hits, hamming, ocr_overlap, version_key
from core.indexer import run_index
from core.search import Hit, search_page
from core.store import Store, char_mask


def _hit(name, path=None, kind="doc", mtime=1_700_000_000.0, phash=0, snippet="",
         width=1920, height=1080):
    path = path or f"C:\\docs\\{name}"
    return Hit(
        file_id=abs(hash((path, name))) % 100000,
        path=path, kind=kind, score=0.5, snippet=snippet, name=name,
        ord=0, mtime=mtime, size=1000, phash=phash, width=width, height=height,
    )


class NameIndexTests(unittest.TestCase):
    def _index(self, names):
        rows = [
            {
                "id": position + 1,
                "path": f"C:\\docs\\{name}",
                "name": name,
                "char_mask": char_mask(name),
                "kind": "doc",
                "size": 100,
                "mtime": 1_700_000_000.0 + position,
            }
            for position, name in enumerate(names)
        ]
        return nameindex.NameIndex.from_rows(rows)

    def test_literal_match_ranks_above_subsequence(self):
        index = self._index(["invoice-march.pdf", "i-n-v-o-i-c-e.txt"])
        hits = index.search("invoice", 5)
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0][0], 1)  # the literal one wins

    def test_abbreviation_still_matches(self):
        index = self._index(["README.md", "report.txt"])
        ids = [file_id for file_id, _ in index.search("rdme", 5)]
        self.assertEqual(ids, [1])

    def test_no_match_returns_nothing(self):
        index = self._index(["README.md"])
        self.assertEqual(index.search("zzz", 5), [])

    def test_case_and_empty_query(self):
        index = self._index(["Report.PDF"])
        self.assertTrue(index.search("REPORT", 5))
        self.assertEqual(index.search("   ", 5), [])

    def test_search_never_returns_more_than_limit(self):
        index = self._index([f"report-{n}.pdf" for n in range(50)])
        self.assertEqual(len(index.search("report", 7)), 7)

    def test_cyrillic_names(self):
        index = self._index(["смета-март.xlsx", "договор.pdf"])
        self.assertEqual([file_id for file_id, _ in index.search("смета", 5)], [1])

    def test_empty_index_is_safe(self):
        index = nameindex.NameIndex.from_rows([])
        self.assertEqual(len(index), 0)
        self.assertEqual(index.search("anything", 5), [])


class FileVectorTests(unittest.TestCase):
    """One binary vector per file, searched by a plain matvec (schema v9).

    The old pack tests pinned a memory-mapped side file and a hot delta; with
    one 48-byte row per file there is nothing to map, so what matters now is
    that the bits round-trip, that a re-index replaces the row, and that the
    result is a file id rather than a chunk id.
    """

    def test_bits_round_trip_at_48_bytes(self):
        vector = np.zeros(384, dtype=np.float32)
        vector[0] = 0.5
        vector[1] = -0.5
        vector[383] = 1.0
        bits = filevec.to_bits(vector)
        self.assertEqual(len(bits), 48)
        self.assertEqual(filevec.packed_size(384), 48)
        back = filevec.from_bits(bits, 384)
        np.testing.assert_array_equal(back, np.where(vector > 0, 1.0, -1.0))

    def test_mean_pool_is_the_document_direction(self):
        chunks = np.array([[1.0, -1.0], [1.0, 1.0]], dtype=np.float32)
        pooled = filevec.mean_pool(chunks)
        np.testing.assert_allclose(pooled, [1.0, 0.0])
        single = np.array([0.3, -0.7], dtype=np.float32)
        np.testing.assert_allclose(filevec.mean_pool(single), single)

    def test_store_knn_answers_with_file_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "index.db")
            try:
                file_id = store.upsert_file("C:\\a.txt", 10, 1.0, "x", "doc")
                store.replace_chunks(
                    file_id, ["one", "two"], np.eye(2, 384, dtype=np.float32)
                )
                store.commit()

                self.assertEqual(store.vector_backend, "filebin")
                self.assertTrue(store.vector_is_file_level)
                self.assertEqual(
                    store.conn.execute("SELECT COUNT(*) FROM file_vectors").fetchone()[0], 1
                )
                hits = store.knn(np.eye(1, 384, 0, dtype=np.float32)[0], 3)
                self.assertEqual([hit[0] for hit in hits], [file_id])
            finally:
                store.close()

    def test_reindex_replaces_the_row_and_the_matrix(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "index.db")
            try:
                file_id = store.upsert_file("C:\\a.txt", 10, 1.0, "x", "doc")
                store.replace_chunks(file_id, ["one"], np.eye(1, 384, 0, dtype=np.float32))
                store.commit()
                first = store.knn(np.eye(1, 384, 0, dtype=np.float32)[0], 1)
                self.assertEqual(len(first), 1)

                # The same file now points at a different direction; the old
                # vector must be gone, not merged with the new one.
                store.replace_chunks(file_id, ["two"], np.eye(1, 384, 1, dtype=np.float32))
                store.commit()
                self.assertEqual(
                    store.conn.execute("SELECT COUNT(*) FROM file_vectors").fetchone()[0], 1
                )
                hits = store.knn(np.eye(1, 384, 0, dtype=np.float32)[0], 1)
                self.assertEqual([hit[0] for hit in hits], [file_id])
                # Anti-parallel now: the old row is gone, not averaged with the new one.
                self.assertAlmostEqual(hits[0][1], -1.0, places=4)
            finally:
                store.close()

    def test_header_is_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.vec"
            path.write_bytes(b"not a pack at all")
            self.assertIsNone(vectors.Pack.open(path, 384))


class VersionTests(unittest.TestCase):
    def test_version_suffixes_collapse(self):
        same = ["resume.docx", "resume_v2.docx", "resume-final.docx", "resume (1).docx",
                "resume_копия.docx", "resume 2024-05-01.docx"]
        keys = {version_key(name) for name in same}
        self.assertEqual(keys, {"resume"})

    def test_different_documents_stay_apart(self):
        self.assertNotEqual(version_key("report.docx"), version_key("report_2024.docx"))
        self.assertNotEqual(version_key("invoice.docx"), version_key("budget.docx"))

    def test_cyrillic_version_marker(self):
        self.assertEqual(version_key("отчет_версия3.docx"), version_key("отчет.docx"))
        self.assertEqual(version_key("итоговый_отчет_в2.docx"), version_key("итоговый_отчет.docx"))

    def test_leading_generic_word_is_not_stripped_to_nothing(self):
        # "итоговый" alone must not collapse to an empty key that merges the world.
        self.assertTrue(version_key("итоговый.docx"))

    def test_versions_group_in_results(self):
        hits = [_hit("resume_v2.docx"), _hit("resume.docx"), _hit("budget.xlsx")]
        groups = group_hits(hits)
        self.assertEqual(len(groups), 2)
        merged = [group for group in groups if group.size > 1]
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].representative.name, "resume_v2.docx")

    def test_different_folders_do_not_merge(self):
        left = _hit("resume.docx", path="C:\\a\\resume.docx")
        right = _hit("resume.docx", path="C:\\b\\resume.docx")
        self.assertEqual(len(group_hits([left, right])), 2)


class CopyTests(unittest.TestCase):
    """The same file copied into two folders is one answer, not two."""

    def _hits(self, left_sha, right_sha, left="a.txt", right="a.txt"):
        first = _hit(left, path=f"C:\\one\\{left}", kind="text")
        second = _hit(right, path=f"C:\\two\\{right}", kind="text")
        hashes = {first.file_id: left_sha, second.file_id: right_sha}
        return [first, second], hashes

    def test_identical_copy_in_another_folder_collapses(self):
        hits, hashes = self._hits("deadbeef", "deadbeef")
        groups = group_hits(hits, hashes=hashes)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].reason, "copy")
        self.assertEqual(groups[0].size, 2)

    def test_different_content_does_not_collapse(self):
        hits, hashes = self._hits("deadbeef", "cafebabe")
        self.assertEqual(len(group_hits(hits, hashes=hashes)), 2)

    def test_same_bytes_under_another_name_does_not_collapse(self):
        hits, hashes = self._hits("deadbeef", "deadbeef", left="a.txt", right="b.txt")
        self.assertEqual(len(group_hits(hits, hashes=hashes)), 2)

    def test_without_hashes_nothing_changes(self):
        hits, _ = self._hits("deadbeef", "deadbeef")
        self.assertEqual(len(group_hits(hits)), 2)


class BurstTests(unittest.TestCase):
    def test_identical_screen_in_a_burst_is_collapsed(self):
        base = 1_700_000_000.0
        hits = [
            _hit(f"shot{n}.png", path=f"C:\\screens\\shot{n}.png", kind="image",
                 mtime=base + n * 30, phash=0b1010_1010_1010_1010,
                 snippet="login form email password")
            for n in range(4)
        ]
        groups = group_hits(hits)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].size, 4)
        self.assertEqual(groups[0].reason, "burst")

    def test_distant_in_time_is_not_a_burst(self):
        base = 1_700_000_000.0
        first = _hit("a.png", path="C:\\screens\\a.png", kind="image", mtime=base,
                     phash=12345, snippet="same text here")
        second = _hit("b.png", path="C:\\screens\\b.png", kind="image",
                      mtime=base + 7200, phash=12345, snippet="same text here")
        self.assertEqual(len(group_hits([first, second])), 2)

    def test_visually_different_is_not_a_burst(self):
        base = 1_700_000_000.0
        first = _hit("a.png", path="C:\\screens\\a.png", kind="image", mtime=base,
                     phash=0, snippet="same text here")
        second = _hit("b.png", path="C:\\screens\\b.png", kind="image", mtime=base + 10,
                      phash=(1 << 40) - 1, snippet="same text here")
        self.assertEqual(len(group_hits([first, second])), 2)

    def test_different_text_is_not_a_burst(self):
        base = 1_700_000_000.0
        first = _hit("a.png", path="C:\\screens\\a.png", kind="image", mtime=base,
                     phash=999, snippet="completely different words")
        second = _hit("b.png", path="C:\\screens\\b.png", kind="image", mtime=base + 10,
                      phash=999, snippet="nothing in common at all")
        self.assertEqual(len(group_hits([first, second])), 2)

    def test_hamming_and_overlap_helpers(self):
        self.assertEqual(hamming(0b1010, 0b1010), 0)
        self.assertEqual(hamming(0b1010, 0b0101), 4)
        self.assertEqual(ocr_overlap("", "x"), 0.0)
        self.assertEqual(ocr_overlap("", ""), 1.0)
        self.assertEqual(ocr_overlap("a b", "a b"), 1.0)

    def test_dhash_is_stable_for_a_flat_image(self):
        from PIL import Image

        flat = Image.new("RGB", (64, 64), "white")
        self.assertEqual(dhash(flat), dhash(flat.copy()))

    def test_icon_set_at_several_sizes_is_not_a_burst(self):
        """dHash resizes to 9x8 first, so one icon exported at 30..1024 px is
        a hash collision. Same folder, same minute, no OCR text — and it must
        still stay four rows: they are four files the user exported."""
        base = 1_700_000_000.0
        hits = [
            _hit(f"icon_{side}.png", path=f"C:\\icons\\icon_{side}.png", kind="image",
                 mtime=base + index, phash=0b1010_1010, width=side, height=side)
            for index, side in enumerate((32, 48, 512, 1024))
        ]
        self.assertEqual(len(group_hits(hits)), 4)

    def test_sprite_frames_are_not_a_burst(self):
        """Animation frames are near-identical by construction and far below
        screenshot size; collapsing them would hide forty real files."""
        base = 1_700_000_000.0
        hits = [
            _hit(f"walk_{n}.png", path=f"C:\\sprites\\walk_{n}.png", kind="image",
                 mtime=base + n * 5, phash=0b1111_0000, width=32, height=32)
            for n in range(6)
        ]
        self.assertEqual(len(group_hits(hits)), 6)

    def test_text_free_pair_needs_a_near_exact_hash(self):
        # phash 0 means "no hash recorded", so the fixtures use a real one.
        base = 1_700_000_000.0
        first = _hit("a.png", path="C:\\screens\\a.png", kind="image", mtime=base,
                     phash=0b1010_1010)
        close = _hit("b.png", path="C:\\screens\\b.png", kind="image",
                     mtime=base + 30, phash=0b1010_0101)      # 4 bits apart
        same = _hit("c.png", path="C:\\screens\\c.png", kind="image",
                    mtime=base + 60, phash=0b1010_1010)       # identical
        self.assertEqual(len(group_hits([first, close])), 2)
        self.assertEqual(len(group_hits([first, same])), 1)

    def test_same_artwork_in_a_different_folder_is_not_a_burst(self):
        base = 1_700_000_000.0
        first = _hit("a.png", path="C:\\one\\a.png", kind="image", mtime=base, phash=7,
                     snippet="shared words")
        second = _hit("b.png", path="C:\\two\\b.png", kind="image", mtime=base + 30, phash=7,
                      snippet="shared words")
        self.assertEqual(len(group_hits([first, second])), 2)


class EndToEndTests(unittest.TestCase):
    def test_search_page_reports_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            for name in ("resume.txt", "resume_v2.txt", "budget.txt"):
                (root / name).write_text("search text about planning and money", encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            try:
                run_index([root], config, store, embedder)
                page = search_page(store, embedder, "planning", limit=10)
                self.assertGreaterEqual(page.groups, 1)
                self.assertLessEqual(page.groups, page.total)
            finally:
                store.close()

    def test_identical_screenshots_index_as_one_burst(self):
        """Three saves of the same window, a minute apart, must be one row."""
        from PIL import Image, ImageDraw

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shots"
            root.mkdir()
            for index in range(3):
                image = Image.new("RGB", (1280, 800), "white")
                draw = ImageDraw.Draw(image)
                draw.rectangle((40, 40, 1240, 160), fill="#dde7f5")
                draw.text((60, 60), "Invoice 4821 confirmed", fill="black")
                # A small clock in the corner: the screenshots differ byte for
                # byte (so the duplicate filter keeps them) while the layout
                # hash stays within a few bits, which is exactly the burst case.
                draw.text((1200, 760), f"12:0{index}", fill="black")
                path = root / f"shot-{index}.png"
                image.save(path)
                # Same screen, minutes apart, inside one burst window.
                stamp = 1_700_000_000.0 + index * 90
                import os
                os.utime(path, (stamp, stamp))

            config = IndexConfig(home=Path(tmp) / "home")
            config.ocr_enabled = False          # keep the test fast and deterministic
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            try:
                run_index([root], config, store, embedder)
                page = search_page(store, embedder, "type:image", limit=10)
                self.assertEqual(page.total, 1, "three saves of one screen are one result")
                self.assertEqual(page.hits[0].group_size, 3)
                self.assertEqual(page.hits[0].group_reason, "burst")
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
