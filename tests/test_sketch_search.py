"""The sketch two-phase search: results must never differ from exact.

The acceptance test bounds the estimator error, and whenever it does not
clear the margin the search falls back to the exact BLAS pass — so equality
with exact results is a property of the design, tested here on both a
clustered corpus (acceptance fires) and an adversarially flat one (fallback
fires on every query).
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

import core.vectors as vecstore
from core.vectors import SKETCH_MIN_ROWS, Pack, build_pack


def clustered_rows(count: int, dim: int = 384, clusters: int = 400, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centers = rng.standard_normal((clusters, dim), dtype=np.float32)
    rows = centers[rng.integers(0, clusters, count)] + rng.standard_normal((count, dim), dtype=np.float32) * 0.3
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    return rows.astype(np.float32)


def flat_rows(count: int, dim: int = 384, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    rows = rng.standard_normal((count, dim), dtype=np.float32)
    rows /= np.linalg.norm(rows, axis=1, keepdims=True)
    return rows.astype(np.float32)


def make_pack(tmp: str, rows: np.ndarray) -> Pack:
    db = Path(tmp) / "index.db"
    sqlite3 = __import__("sqlite3")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE vectors (id INTEGER PRIMARY KEY, owner_type TEXT, "
        "owner_id INTEGER, dim INTEGER, vec BLOB)"
    )
    conn.executemany(
        "INSERT INTO vectors(id, owner_type, owner_id, dim, vec) VALUES(?, 'chunk', ?, ?, ?)",
        [(i + 1, i + 1, rows.shape[1], rows[i].tobytes()) for i in range(len(rows))],
    )
    conn.commit()
    pack = build_pack(conn, db, rows.shape[1])
    conn.close()
    assert pack is not None
    return pack


class SketchSearchTests(unittest.TestCase):
    COUNT = SKETCH_MIN_ROWS  # exactly the threshold: the two-phase path runs

    def test_accepted_results_equal_exact_on_clustered_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, clustered_rows(self.COUNT))
            try:
                rng = np.random.default_rng(11)
                for _ in range(20):
                    query = clustered_rows(1, seed=int(rng.integers(0, 1 << 30)))[0]
                    exact = pack._search_exact(query, 50)
                    fast = pack._search_sketch(query, 50)
                    self.assertEqual([fid for fid, _ in exact], [fid for fid, _ in fast])
                    # The pack really carries the sketch payload.
                    self.assertTrue(pack.sketches is not None)
            finally:
                pack.close()

    def test_fallback_results_equal_exact_on_flat_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, flat_rows(self.COUNT))
            try:
                rng = np.random.default_rng(13)
                for _ in range(20):
                    query = flat_rows(1, seed=int(rng.integers(0, 1 << 30)))[0]
                    exact = pack._search_exact(query, 50)
                    fast = pack._search_sketch(query, 50)
                    # On a flat score distribution the margin cannot clear, so
                    # the sketch path must delegate to exact, byte for byte.
                    self.assertEqual(exact, fast)
            finally:
                pack.close()

    def test_flat_data_turns_the_sketch_off_after_three_queries(self):
        # On the real index the margin is -0.78 against a +0.32 threshold, so
        # nothing is ever accepted. Paying 5-9 ms of popcount per query to keep
        # proving that is the only thing the sketch does there.
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, flat_rows(self.COUNT))
            try:
                rng = np.random.default_rng(17)
                for _ in range(3):
                    pack.search(flat_rows(1, seed=int(rng.integers(0, 1 << 30)))[0], 20)
                self.assertEqual(pack._sketch_accepts, 0)
                self.assertEqual(pack._sketch_mode, "off")
                # Off means the next query never even builds the popcount.
                before = pack._sketch_tries
                pack.search(flat_rows(1, seed=5)[0], 20)
                self.assertEqual(pack._sketch_tries, before)
            finally:
                pack.close()

    def test_small_packs_skip_the_sketch_entirely(self):
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, clustered_rows(500))
            try:
                self.assertLess(pack.count, SKETCH_MIN_ROWS)
                rng = np.random.default_rng(3)
                for _ in range(5):
                    query = clustered_rows(1, seed=int(rng.integers(0, 1 << 30)))[0]
                    self.assertEqual(pack._search_exact(query, 10), pack.search(query, 10))
            finally:
                pack.close()

    def test_version_bump_rebuilds_old_packs(self):
        # A v1 pack (no sketch block) must be rejected by open() so the cache
        # rebuilds it as v3 instead of answering from a truncated layout.
        rows = clustered_rows(1000)
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, rows)
            path = pack.path
            pack.close()
            # rewrite the header with the old version and drop the sketch tail
            raw = bytearray(path.read_bytes())
            raw[8:12] = (1).to_bytes(4, "little")
            old_size = 40 + 1000 * 8 + 1000 * 384 * 4
            path.write_bytes(bytes(raw[:old_size]))
            self.assertIsNone(Pack.open(path, 384))

    def test_expanded_rerank_on_a_deduplicated_pack(self):
        """The acceptance path must survive duplicates and keep < count.

        `_expand` maps an original row through its unique row into the rerank
        scores; indexed by unique id directly it read another row's score —
        invisible while every test pack was duplicate-free and acceptance
        always fell back to exact, and an IndexError on a real pack (263k
        rows, 28% duplicates) the first time the sketch passed its own bound.
        Orthogonal clusters put the nearest excluded sketch far away, so the
        bound clears and the two-phase result really is the expanded one.
        """
        rng = np.random.default_rng(5)
        dim = 384
        centers = rng.standard_normal((4, dim), dtype=np.float32)
        # Gram-Schmidt: mutually orthogonal centers, so a query at center 0
        # scores ~1.0 there and ~0 everywhere else.
        for i in range(1, len(centers)):
            for j in range(i):
                centers[i] -= (centers[i] @ centers[j] / (centers[j] @ centers[j])) * centers[j]
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
        centers = centers + rng.standard_normal(centers.shape, dtype=np.float32) * 0.01
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
        rows = np.repeat(centers, 30_000, axis=0)          # 120k rows, byte-identical duplicates
        with tempfile.TemporaryDirectory() as tmp:
            pack = make_pack(tmp, rows.astype(np.float32))
            try:
                self.assertLess(pack.count, pack.orig_count, "the pack must carry duplicates")
                query = rows[0].copy()
                exact = pack._search_exact(query, 50)
                fast = pack._search_sketch(query, 50)
                self.assertEqual([fid for fid, _ in exact], [fid for fid, _ in fast])
                for (_, s_exact), (_, s_fast) in zip(exact, fast):
                    self.assertAlmostEqual(s_exact, s_fast, places=4)
            finally:
                pack.close()


if __name__ == "__main__":
    unittest.main()
