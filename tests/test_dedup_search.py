"""Pack v4 dedup: results must match a full scan over the original rows,
including duplicate multiplicity — duplicates keep their slots in the top-k."""

from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from core.vectors import Pack, build_pack


def make_rows(count: int, dup_ratio: float, dim: int = 384, seed: int = 7) -> tuple[np.ndarray, np.ndarray]:
    """Rows where a `dup_ratio` fraction are byte-copies of earlier rows."""
    rng = np.random.default_rng(seed)
    unique_count = int(count * (1 - dup_ratio))
    unique = rng.standard_normal((unique_count, dim), dtype=np.float32)
    unique /= np.linalg.norm(unique, axis=1, keepdims=True)
    if dup_ratio <= 0:
        rows = unique
    else:
        rows = unique[rng.integers(0, unique_count, count)]
        rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    return np.ascontiguousarray(rows, dtype=np.float32), unique


def make_pack(tmp: str, rows: np.ndarray):
    sqlite3_mod = sqlite3
    db = Path(tmp) / "index.db"
    conn = sqlite3_mod.connect(db)
    conn.row_factory = sqlite3_mod.Row
    conn.execute(
        "CREATE TABLE vectors (id INTEGER PRIMARY KEY, owner_type TEXT, "
        "owner_id INTEGER, dim INTEGER, vec BLOB)"
    )
    conn.executemany(
        "INSERT INTO vectors(id, owner_type, owner_id, dim, vec) VALUES(?,'chunk',?,?,?)",
        [(i + 1, i + 1, rows.shape[1], rows[i].tobytes()) for i in range(len(rows))],
    )
    conn.commit()
    pack = build_pack(conn, db, rows.shape[1])
    conn.close()
    return pack


def reference_topk(rows: np.ndarray, query: np.ndarray, k: int):
    scores = rows @ query
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(int(i + 1), float(scores[i])) for i in top]


class DedupEquivalenceTests(unittest.TestCase):
    COUNT = 60_000  # below the sketch threshold: pure dedup path

    def test_results_match_full_scan_with_duplicates(self):
        rows, _ = make_rows(self.COUNT, dup_ratio=0.28)
        rng = np.random.default_rng(5)
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, rows)
            try:
                self.assertLess(pack.count, self.COUNT)  # dedup actually happened
                for _ in range(8):
                    query = rows[rng.integers(0, len(rows))]
                    query = query / np.linalg.norm(query)
                    got = pack.search(query, 50)
                    want = reference_topk(rows, query, 50)
                    # Same scores in the same order; ids may swap only between
                    # rows sharing an identical score (ties).
                    self.assertEqual(
                        [round(s, 6) for _, s in got],
                        [round(s, 6) for _, s in want],
                    )
                    score_to_want_ids: dict[float, set[int]] = {}
                    for fid, score in want:
                        score_to_want_ids.setdefault(round(score, 6), set()).add(fid)
                    for fid, score in got:
                        self.assertIn(fid, score_to_want_ids[round(score, 6)])
            finally:
                pack.close()

    def test_unique_stats_and_smaller_file(self):
        rows, _ = make_rows(self.COUNT, dup_ratio=0.28)
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, rows)
            try:
                stats = pack.stats()
                self.assertEqual(stats["count"], self.COUNT)
                self.assertLess(stats["unique"], self.COUNT)
            finally:
                pack.close()

    def test_no_duplicates_degenerates_to_full_scan(self):
        rows, _ = make_rows(self.COUNT, dup_ratio=0.0)
        rng = np.random.default_rng(9)
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, rows)
            try:
                self.assertEqual(pack.count, self.COUNT)
                query = rows[0] / np.linalg.norm(rows[0])
                got = pack.search(query, 25)
                want = reference_topk(rows, query, 25)
                self.assertEqual([fid for fid, _ in got], [fid for fid, _ in want])
            finally:
                pack.close()

    def test_sketch_composes_with_dedup_at_scale(self):
        rows, unique = make_rows(237_024, dup_ratio=0.28)
        rng = np.random.default_rng(11)
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, rows)
            try:
                for _ in range(6):
                    base = unique[rng.integers(0, len(unique))]
                    query = base + rng.standard_normal(len(base), dtype=np.float32) * 0.25
                    query = (query / np.linalg.norm(query)).astype(np.float32)
                    got = pack.search(query, 50)
                    want = reference_topk(rows, query, 50)
                    self.assertEqual(
                        [round(s, 5) for _, s in got],
                        [round(s, 5) for _, s in want],
                    )
            finally:
                pack.close()


class MaxIdDuplicateTests(unittest.TestCase):
    def test_max_id_row_being_a_duplicate_does_not_break_validation(self):
        # The highest chunk id duplicates row 1: its vector is absent from the
        # unique id list, and the staleness key (rows, max id) must still match
        # — otherwise every acquire would rebuild and fall to the blob path.
        rng = np.random.default_rng(21)
        rows = rng.standard_normal((50, 384), dtype=np.float32)
        rows[49] = rows[0]  # max id duplicates the first row
        rows /= np.linalg.norm(rows, axis=1, keepdims=True)
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, rows)
            try:
                from core.vectors import pack_key
                conn = sqlite3.connect(Path(tmp) / "index.db")
                conn.row_factory = sqlite3.Row
                key = pack_key(conn)
                conn.close()
                query = rows[1] / np.linalg.norm(rows[1])
                got = pack.search(query, 5)
                self.assertEqual(
                    (pack.orig_count, int(pack.orig_ids.max())), key
                )
                self.assertEqual(len(got), 5)
            finally:
                pack.close()


class DedupSpeedTests(unittest.TestCase):
    def test_dedup_scan_is_faster_and_exact(self):
        rows, _ = make_rows(237_024, dup_ratio=0.28, seed=3)
        rng = np.random.default_rng(4)
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, rows)
            try:
                query = rows[0] / np.linalg.norm(rows[0])
                # Warm past the sketch self-tuning: it tries the shortcut on
                # the first queries, measures the acceptance rate, and reverts
                # to exact when the shortcut loses — which it does here, where
                # dedup already shrank the exact pass.
                for _ in range(15):
                    pack.search(query, 50)
                self.assertEqual(pack._sketch_mode, "off")
                t0 = time.perf_counter()
                for _ in range(7):
                    got = pack.search(query, 50)
                dedup_ms = (time.perf_counter() - t0) / 7 * 1000
                want = reference_topk(rows, query, 50)
                self.assertEqual(
                    [round(s, 5) for _, s in got], [round(s, 5) for _, s in want]
                )
                # The scan runs over unique rows only: it must beat a full
                # pass by at least the duplicate ratio, with margin for noise.
                self.assertLess(dedup_ms, 12.0 * (1 - 0.28) + 2.5)
                print(f"\ndedup exact scan: {dedup_ms:.2f} ms (full-scan floor 12 ms)")
            finally:
                pack.close()


if __name__ == "__main__":
    unittest.main()
