"""The pack's file name carries its format version, and a locked file is survivable.

Both exist because of one measured failure. Bumping `PACK_VERSION` to 4 while an
older process still had `index.db.vec` mapped meant `Path.replace` could not
land (Windows refuses to replace a mapped file), `build_pack` returned None, and
every text query fell back to reading all vectors out of SQLite: 2.9 s against
12 ms, on the real index, twice in a row. Versioned names make the two
processes touch different files; the temp fallback keeps a same-version rebuild
from degrading to the blob path when someone else holds the file.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np

import core.vectors as vecstore


def make_db(path: Path, rows: int, dim: int = 8) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE vectors (id INTEGER PRIMARY KEY, owner_type TEXT, "
        "owner_id INTEGER, dim INTEGER, vec BLOB)"
    )
    rng = np.random.default_rng(3)
    data = rng.standard_normal((rows, dim)).astype(np.float32)
    conn.executemany(
        "INSERT INTO vectors(id, owner_type, owner_id, dim, vec) VALUES(?, 'chunk', ?, ?, ?)",
        [(i + 1, i + 1, dim, data[i].tobytes()) for i in range(rows)],
    )
    conn.commit()
    return conn


def add_row(conn: sqlite3.Connection, dim: int = 8) -> None:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM vectors").fetchone()[0]
    vector = np.ones(dim, dtype=np.float32).tobytes()
    conn.execute(
        "INSERT INTO vectors(id, owner_type, owner_id, dim, vec) VALUES(?, 'chunk', ?, ?, ?)",
        (row, row, dim, vector),
    )
    conn.commit()


class PackNameTests(unittest.TestCase):
    def test_the_name_carries_the_format_version(self):
        name = vecstore.pack_path(Path("C:/data/index.db")).name
        self.assertEqual(name, f"index.db.v{vecstore.PACK_VERSION}.vec")

    def test_a_pack_from_another_version_is_not_picked_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            legacy = Path(str(db) + ".vec")  # the pre-versioning name
            legacy.write_bytes(vecstore.MAGIC + b"\x00" * 32)
            self.assertIsNone(vecstore.Pack.open(vecstore.pack_path(db), 8))
            self.assertEqual([p.name for p in vecstore.stale_packs(db)], ["index.db.vec"])

    def test_a_stale_pack_is_swept_once_the_new_one_lands(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            legacy = Path(str(db) + ".vec")
            legacy.write_bytes(b"old pack, 364 MB in the real case")
            conn = make_db(db, 3)
            pack = vecstore.build_pack(conn, db, 8)
            self.assertIsNotNone(pack)
            pack.close()
            self.assertFalse(legacy.exists(), "the 300 MB of a dead version stays behind")
            self.assertTrue(vecstore.pack_path(db).exists())
            conn.close()

    def test_drop_pack_removes_every_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            legacy = Path(str(db) + ".vec")
            legacy.write_bytes(b"old")
            conn = make_db(db, 2)
            vecstore.build_pack(conn, db, 8).close()
            conn.close()
            self.assertTrue(vecstore.drop_pack(db))
            self.assertFalse(legacy.exists())
            self.assertFalse(vecstore.pack_path(db).exists())


class LockedPackTests(unittest.TestCase):
    def test_a_locked_target_still_yields_the_new_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            conn = make_db(db, 4)
            first = vecstore.build_pack(conn, db, 8)
            self.assertEqual(first.orig_count, 4)
            first.close()

            # Python's open() takes no FILE_SHARE_DELETE, so the target cannot
            # be replaced while this handle lives — exactly what another
            # process's mapping does to us on Windows.
            with open(vecstore.pack_path(db), "rb"):
                add_row(conn)
                rebuilt = vecstore.build_pack(conn, db, 8)
                self.assertIsNotNone(rebuilt, "a locked file must not cost us the pack")
                self.assertEqual(rebuilt.orig_count, 5)
                ids = {chunk_id for chunk_id, _ in rebuilt.search(np.ones(8, np.float32), 5)}
                self.assertEqual(len(ids), 5)
                rebuilt.close()

            # Once the handle is gone, the next build lands on the real name
            # and sweeps the temp it was serving from.
            add_row(conn)
            landed = vecstore.build_pack(conn, db, 8)
            self.assertEqual(landed.orig_count, 6)
            landed.close()
            leftovers = list(Path(tmp).glob("index.db.v*.vec.*.tmp"))
            self.assertEqual(leftovers, [], "the served-from-temp file is never cleaned up")
            conn.close()

    def test_a_failed_build_leaves_no_temp_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            conn = make_db(db, 2)
            conn.execute("ALTER TABLE vectors RENAME TO gone")
            conn.commit()
            self.assertIsNone(vecstore.build_pack(conn, db, 8))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])
            conn.close()


if __name__ == "__main__":
    unittest.main()
