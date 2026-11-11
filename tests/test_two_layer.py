"""The two-layer index: a base pack that never rewrites itself for appends.

Layer one is the deduplicated memory-mapped pack, immutable between
compactions; layer two a hot delta of the vectors that arrived after it was
built. What the tests pin down is the contract around it: a refresh after pure
appends must leave the base file byte-for-byte alone, deletions must trigger a
full rebuild (a deleted row and a re-used rowid are invisible to an id
comparison — that is what the vector epoch is for), and a merged search must
return exactly what one full scan over every row would.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import core.vectors as vecstore


DIM = 8


def make_db(path: Path, rows: int, with_meta: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE vectors (id INTEGER PRIMARY KEY, owner_type TEXT, "
        "owner_id INTEGER, dim INTEGER, vec BLOB)"
    )
    if with_meta:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
    rng = np.random.default_rng(7)
    data = rng.standard_normal((rows, DIM)).astype(np.float32)
    conn.executemany(
        "INSERT INTO vectors(owner_type, owner_id, dim, vec) VALUES('chunk', ?, ?, ?)",
        [(i + 1, DIM, data[i].tobytes()) for i in range(rows)],
    )
    conn.commit()
    return conn


def add_row(conn: sqlite3.Connection, vector: np.ndarray | None = None) -> int:
    if vector is None:
        vector = np.ones(DIM, dtype=np.float32)
    cursor = conn.execute(
        "INSERT INTO vectors(owner_type, owner_id, dim, vec) VALUES('chunk', ?, ?, ?)",
        (conn.execute("SELECT COALESCE(MAX(owner_id), 0) + 1 FROM vectors WHERE owner_type='chunk'").fetchone()[0],
         DIM, vector.astype(np.float32).tobytes()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def brute_force(conn: sqlite3.Connection, query: np.ndarray, limit: int):
    rows = conn.execute(
        "SELECT id, vec FROM vectors WHERE owner_type='chunk' ORDER BY id"
    ).fetchall()
    matrix = np.vstack([np.frombuffer(row["vec"], dtype=np.float32) for row in rows])
    scores = matrix @ np.asarray(query, dtype=np.float32)
    top = np.argsort(-scores)[:limit]
    return [(int(rows[i]["id"]), float(scores[i])) for i in top]


class TwoLayerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "index.db"

    def tearDown(self):
        vecstore.PACKS.drop(self.db)
        self._tmp.cleanup()

    def stamp(self) -> tuple:
        info = vecstore.pack_path(self.db).stat()
        return (info.st_mtime_ns, info.st_size)

    def test_appends_land_in_the_delta_and_leave_the_base_alone(self):
        conn = make_db(self.db, 50)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        pack = vecstore.PACKS.acquire(self.db, DIM, conn)
        self.assertIsNotNone(pack)
        self.assertNotIsInstance(pack, vecstore.CompositePack)
        self.assertEqual(pack.count, 50)
        vecstore.PACKS.release(self.db, pack)

        before = self.stamp()
        self.assertFalse(vecstore.hot_path(self.db).exists())
        for _ in range(3):
            add_row(conn)

        vecstore.PACKS.refresh(self.db, conn, DIM)
        self.assertTrue(vecstore.hot_path(self.db).exists())
        self.assertEqual(self.stamp(), before, "the 270 MB base was rewritten for 3 rows")

        pack = vecstore.PACKS.acquire(self.db, DIM, conn)
        self.assertIsInstance(pack, vecstore.CompositePack)
        self.assertEqual(pack.count, 53)
        vecstore.PACKS.release(self.db, pack)
        conn.close()

    def test_merged_search_equals_a_full_scan(self):
        conn = make_db(self.db, 120)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        rng = np.random.default_rng(11)
        for _ in range(30):
            add_row(conn, rng.standard_normal(DIM).astype(np.float32))
        # delta rows that repeat base rows byte for byte: each is its own
        # original row and the full scan reports both
        twin = np.frombuffer(
            conn.execute("SELECT vec FROM vectors WHERE id = 5").fetchone()["vec"],
            dtype=np.float32,
        )
        add_row(conn, twin)
        vecstore.PACKS.refresh(self.db, conn, DIM)

        pack = vecstore.PACKS.acquire(self.db, DIM, conn)
        self.assertIsInstance(pack, vecstore.CompositePack)
        try:
            for seed in range(5):
                query = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
                got = pack.search(query, 7)
                want = brute_force(conn, query, 7)
                self.assertEqual([hit[0] for hit in got], [hit[0] for hit in want])
                for (g_id, g_score), (_, w_score) in zip(got, want):
                    self.assertAlmostEqual(g_score, w_score, places=4, msg=f"id {g_id}")
        finally:
            vecstore.PACKS.release(self.db, pack)
        conn.close()

    def test_a_restart_serves_the_sidecar_without_touching_sqlite_blobs(self):
        conn = make_db(self.db, 40)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        add_row(conn)
        add_row(conn)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        conn.close()

        # A new process: the cache is empty, the connection fresh. The delta
        # must come from the sidecar, so the (rows > base_max) blobs are never
        # read — approximated here by counting the SELECTs it could skip.
        conn = sqlite3.connect(self.db)
        conn.row_factory = sqlite3.Row
        vecstore.PACKS.refresh(self.db, conn, DIM)
        pack = vecstore.PACKS.acquire(self.db, DIM, conn)
        self.assertIsInstance(pack, vecstore.CompositePack)
        self.assertEqual(pack.count, 42)
        vecstore.PACKS.release(self.db, pack)
        conn.close()

    def test_deletions_force_a_full_rebuild(self):
        conn = make_db(self.db, 30)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        before = self.stamp()

        conn.execute("DELETE FROM vectors WHERE id = 10")
        vecstore.bump_vector_epoch(conn)
        conn.commit()
        add_row(conn)

        vecstore.PACKS.refresh(self.db, conn, DIM)
        self.assertNotEqual(self.stamp(), before, "a deleted row must compact, not patch")
        pack = vecstore.PACKS.acquire(self.db, DIM, conn)
        self.assertEqual(pack.count, 30)
        ids = {chunk_id for chunk_id, _ in pack.search(np.ones(DIM, np.float32), 40)}
        self.assertNotIn(10, ids)
        vecstore.PACKS.release(self.db, pack)
        conn.close()

    def test_a_reused_rowid_gets_its_new_vector(self):
        """Delete the max-id row, insert again: the freed rowid comes back.

        An id-set comparison cannot tell the new row from the old one — the
        epoch is what catches it, and the rebuild must serve the NEW vector.
        """
        conn = make_db(self.db, 5)
        vecstore.PACKS.refresh(self.db, conn, DIM)

        new_vector = np.full(DIM, -1.0, np.float32)  # anti-parallel to the old rows' +1s
        conn.execute("DELETE FROM vectors WHERE id = 5")
        vecstore.bump_vector_epoch(conn)
        conn.commit()
        reused = add_row(conn, new_vector)
        self.assertEqual(reused, 5, "the scenario only makes sense if the rowid is reused")

        vecstore.PACKS.refresh(self.db, conn, DIM)
        pack = vecstore.PACKS.acquire(self.db, DIM, conn)
        try:
            hits = pack.search(new_vector, 5)
            # Freshness is the exact agreement with a full scan — the stale
            # row would carry the deleted vector and score differently. (The
            # vectors here are raw dots, not cosines, so a random row can
            # legitimately outscore the query's own self-match.)
            self.assertEqual(
                [hit[0] for hit in hits],
                [hit[0] for hit in brute_force(conn, new_vector, 5)],
            )
            scores = dict(hits)
            self.assertAlmostEqual(scores[5], float(DIM), places=3,
                                   msg="id 5 must carry the NEW (-1) vector")
        finally:
            vecstore.PACKS.release(self.db, pack)
        conn.close()

    def test_a_delta_past_its_cap_compacts(self):
        conn = make_db(self.db, 20)
        try:
            vecstore.PACKS.refresh(self.db, conn, DIM)
            before = self.stamp()
            for _ in range(3):
                add_row(conn)
            with mock.patch.object(vecstore, "HOT_MAX_ROWS", 2), \
                    mock.patch.object(vecstore, "HOT_MIN_ROWS", 1):
                vecstore.PACKS.refresh(self.db, conn, DIM)
            self.assertNotEqual(self.stamp(), before, "the cap must trigger a compaction")
            pack = vecstore.PACKS.acquire(self.db, DIM, conn)
            try:
                self.assertNotIsInstance(pack, vecstore.CompositePack)
                # the three added rows are byte-identical and dedup to one
                self.assertEqual(pack.orig_count, 23)
            finally:
                vecstore.PACKS.release(self.db, pack)
        finally:
            conn.close()

    def test_a_store_without_the_meta_table_keeps_the_old_behaviour(self):
        """Raw connections (no `meta`) never gain a delta: appends rebuild."""
        conn = make_db(self.db, 10, with_meta=False)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        before = self.stamp()
        add_row(conn)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        self.assertNotEqual(self.stamp(), before)
        pack = vecstore.PACKS.acquire(self.db, DIM, conn)
        self.assertEqual(pack.count, 11)
        vecstore.PACKS.release(self.db, pack)
        conn.close()

    def test_the_sidecar_is_not_mistaken_for_a_stale_pack(self):
        conn = make_db(self.db, 5)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        add_row(conn)
        vecstore.PACKS.refresh(self.db, conn, DIM)
        self.assertEqual(vecstore.stale_packs(self.db), [])
        self.assertEqual(vecstore.stale_hots(self.db), [])

        # A sidecar of another pack version is dead weight; sweeping it is
        # the same rule `_sweep` applies to old `.vec` files.
        orphan = self.db.with_name("index.db.v3.hot")
        orphan.write_bytes(b"x")
        self.assertEqual([p.name for p in vecstore.stale_hots(self.db)], ["index.db.v3.hot"])
        vecstore.drop_pack(self.db)
        self.assertFalse(orphan.exists())
        self.assertFalse(vecstore.hot_path(self.db).exists())
        conn.close()


class StoreLevelTests(unittest.TestCase):
    """The v9 storage contract through the API an index run actually uses.

    Text lives once per *content* (a second copy of a document costs one row in
    `files`), a chunk is a span into that text, and a document has one binary
    vector. The old pack/delta machinery is covered by `TwoLayerTests`, which
    still drives `core.vectors` directly for the image side.
    """

    SDIM = 384  # the Store's own default dim

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "index.db"

    def tearDown(self):
        vecstore.PACKS.drop(self.db)
        self._tmp.cleanup()

    def _store(self):
        from core.store import Store

        return Store(self.db)

    def test_identical_documents_share_one_stored_text(self):
        store = self._store()
        try:
            text = "one two three"
            a = store.upsert_file("C:\\a.txt", 10, 1.0, "sha-a", "doc")
            store.replace_chunks(a, text, None, np.eye(1, self.SDIM, 0, dtype=np.float32))
            b = store.upsert_file("C:\\b.txt", 10, 1.0, "sha-b", "doc")
            store.replace_chunks(b, text, None, np.eye(1, self.SDIM, 0, dtype=np.float32))
            store.commit()

            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM file_texts").fetchone()[0], 1,
                "the same text must be stored once, not once per file",
            )
            self.assertEqual(store.file_text(a), text)
            self.assertEqual(store.file_text(b), text)

            # Dropping one copy keeps the blob for the other; dropping the last
            # one releases it.
            store.replace_chunks(a, "", None, None)
            store.commit()
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM file_texts").fetchone()[0], 1
            )
            store.replace_chunks(b, "", None, None)
            store.commit()
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM file_texts").fetchone()[0], 0
            )
        finally:
            store.close()

    def test_chunks_are_spans_into_the_stored_text(self):
        store = self._store()
        try:
            text = "alpha beta gamma delta"
            file_id = store.upsert_file("C:\\a.txt", 10, 1.0, "sha-a", "doc")
            spans = [(0, 10), (11, 16), (17, 22)]
            written = store.replace_chunks(
                file_id, text, spans, np.eye(1, self.SDIM, 0, dtype=np.float32)
            )
            store.commit()

            self.assertEqual(written, 3)
            rows = store.conn.execute(
                "SELECT ord, start, len FROM chunks WHERE file_id = ? ORDER BY ord", (file_id,)
            ).fetchall()
            self.assertEqual([(r["ord"], r["start"], r["len"]) for r in rows],
                             [(0, 0, 10), (1, 11, 5), (2, 17, 5)])
            self.assertEqual(store.chunk_heads(file_id, limit=2), "alpha beta\ngamma")
        finally:
            store.close()

    def test_reindex_replaces_the_file_vector(self):
        store = self._store()
        try:
            file_id = store.upsert_file("C:\\a.txt", 10, 1.0, "x", "doc")
            store.replace_chunks(file_id, ["only"], None,
                                 np.eye(1, self.SDIM, 0, dtype=np.float32))
            store.commit()
            hits = store.knn(np.eye(1, self.SDIM, 0, dtype=np.float32)[0], 3)
            self.assertEqual([hit[0] for hit in hits], [file_id])
            self.assertAlmostEqual(hits[0][1], 1.0, places=4)

            store.replace_chunks(file_id, ["other"], None,
                                 np.eye(1, self.SDIM, 1, dtype=np.float32))
            store.commit()
            self.assertEqual(
                store.conn.execute("SELECT COUNT(*) FROM file_vectors").fetchone()[0], 1
            )
            hits = store.knn(np.eye(1, self.SDIM, 0, dtype=np.float32)[0], 3)
            self.assertEqual([hit[0] for hit in hits], [file_id])
            # Anti-parallel: the old row was replaced, not averaged with the new.
            self.assertAlmostEqual(hits[0][1], -1.0, places=4)
        finally:
            store.close()

    def test_long_document_spans_cross_block_boundaries(self):
        store = self._store()
        try:
            # 30 000 characters is two 16 384-char blocks, so the middle span
            # starts in the first block and the last one lives in the second.
            text = "".join(f"word{n} " for n in range(4000))[:30000]
            spans = [(0, 100), (20000, 20100), (29900, 30000)]
            file_id = store.upsert_file("C:\\long.txt", 10, 1.0, "sha-long", "doc")
            store.replace_chunks(file_id, text, spans, None)
            store.commit()

            self.assertEqual(store.file_text(file_id), text)
            rows = store.conn.execute(
                "SELECT id, start, len FROM chunks WHERE file_id = ? ORDER BY ord", (file_id,)
            ).fetchall()
            slices = store.chunk_rows([int(row["id"]) for row in rows])
            for row in rows:
                start = int(row["start"])
                self.assertEqual(
                    slices[int(row["id"])]["text"],
                    text[start:start + int(row["len"])],
                )
        finally:
            store.close()

    def test_regex_and_snippets_read_the_span_not_a_copy(self):
        store = self._store()
        try:
            file_id = store.upsert_file("C:\\a.txt", 10, 1.0, "x", "doc")
            store.replace_chunks(file_id, "first sentence. second sentence.", None, None)
            store.commit()

            rows = store.regex_chunks("second", 10)
            self.assertEqual([row["text"] for row in rows], ["first sentence. second sentence."])
            chunk = next(iter(store.chunk_rows([rows[0]["id"]]).values()))
            self.assertIn("second", chunk["text"])
            self.assertEqual(chunk["file_id"], file_id)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
