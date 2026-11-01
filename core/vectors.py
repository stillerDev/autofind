"""Vector storage: a memory-mapped pack by default, sqlite-vec on request.

The first implementation kept every chunk vector in a Python-side numpy matrix
built at startup: 217k x 384 float32 is a 333 MB allocation and a 2.6 s cold
start, paid again after every index run. Two replacements were measured on this
machine (217k chunks, Windows 11):

===========================  ===========  ==============  ==============
backend                      cold start   query, 217k    extra disk
===========================  ===========  ==============  ==============
numpy matrix (before)        2 600 ms     21 ms           —
sqlite-vec `vec0`            0 ms         90-255 ms       +333 MB
memory-mapped pack (now)     ~0 ms        12-21 ms        +333 MB
===========================  ===========  ==============  ==============

The pack wins because `np.memmap` maps the matrix instead of copying it: the
first query faults pages in, and the OS keeps them, so the cost is paid once
per boot by the machine rather than once per process. sqlite-vec is correct and
standard, but its exact KNN walks shadow rows one at a time and costs 7x more
per query than one BLAS call — it stays available via `--vector-backend
sqlite-vec` for anyone who wants the vectors in SQL.

The pack is pure cache: `vectors` in SQLite remains the source of truth, so
deleting `index.db.vec` costs a rebuild, never data.
"""

from __future__ import annotations

import sqlite3
import struct
from pathlib import Path

import numpy as np

BACKEND_META = "vector_backend"
VEC_TABLE = "vec_chunks"
PACK_META = "vector_pack_key"

HEADER = struct.Struct("<8sIIQQ")
MAGIC = b"SFVEC\x00\x00\x00"
PACK_VERSION = 1
HEADER_BYTES = HEADER.size


# --------------------------------------------------------------------- sqlite-vec


def available() -> bool:
    """Is the sqlite-vec extension importable?"""
    try:
        import sqlite_vec  # noqa: F401
    except Exception:
        return False
    return True


def load(conn: sqlite3.Connection) -> bool:
    """Attach the extension to `conn`. Returns False when it is not installed."""
    try:
        import sqlite_vec
    except Exception:
        return False
    try:
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        return True
    except (sqlite3.Error, OSError, AttributeError):
        return False


def serialize(vector: np.ndarray) -> bytes:
    """float32 blob, the format vec0 expects for a MATCH parameter."""
    return np.asarray(vector, dtype=np.float32).tobytes()


def create(conn: sqlite3.Connection, dim: int) -> None:
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING vec0("
        f"chunk_id INTEGER PRIMARY KEY, embedding FLOAT[{dim}] distance_metric=cosine)"
    )


def insert_chunks(conn: sqlite3.Connection, rows: list[tuple[int, np.ndarray]]) -> None:
    if not rows:
        return
    conn.executemany(
        f"INSERT OR REPLACE INTO {VEC_TABLE}(chunk_id, embedding) VALUES (?, ?)",
        [(chunk_id, serialize(vector)) for chunk_id, vector in rows],
    )


def delete_chunks(conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    conn.executemany(f"DELETE FROM {VEC_TABLE} WHERE chunk_id = ?", [(i,) for i in chunk_ids])


def knn(conn: sqlite3.Connection, vector: np.ndarray, limit: int) -> list[tuple[int, float]]:
    """Cosine similarity, highest first — same contract as the numpy path."""
    if limit <= 0:
        return []
    try:
        rows = conn.execute(
            f"SELECT chunk_id, distance FROM {VEC_TABLE} WHERE embedding MATCH ? AND k = ?",
            (serialize(vector), limit),
        ).fetchall()
    except sqlite3.Error:
        return []
    return [(int(row[0]), 1.0 - float(row[1])) for row in rows]


def count(conn: sqlite3.Connection) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {VEC_TABLE}").fetchone()[0])
    except sqlite3.Error:
        return 0


def migrate_from_blobs(conn: sqlite3.Connection, dim: int, batch: int = 4096) -> int:
    """Mirror `vectors` into vec0. The blobs are the source of truth and stay.

    Keeping both copies costs 333 MB of extra disk, which is the price of the
    `sqlite-vec` backend being switchable: emptying `vectors` after the copy
    would make `--vector-backend pack` silently return nothing. The default
    backend does not pay this, because it never creates a vec0 table.
    """
    try:
        total = int(conn.execute("SELECT COUNT(*) FROM vectors WHERE owner_type='chunk'").fetchone()[0])
    except sqlite3.Error:
        return 0
    if not total:
        return 0

    create(conn, dim)
    cursor = conn.execute(
        "SELECT id, vec FROM vectors WHERE owner_type='chunk' ORDER BY id"
    )
    moved = 0
    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            break
        conn.executemany(
            f"INSERT OR REPLACE INTO {VEC_TABLE}(chunk_id, embedding) VALUES (?, ?)",
            [(int(row["id"]), row["vec"]) for row in rows],
        )
        moved += len(rows)
    conn.commit()
    return moved


# --------------------------------------------------------------------- pack file


class Pack:
    """A memory-mapped float32 matrix with its chunk ids.

    Nothing is copied on open: `np.ndarray(buffer=mmap)` gives views straight
    into the mapped file, so the 333 MB never becomes anonymous memory and a
    second process reuses the pages the first one already faulted in.
    """

    __slots__ = ("path", "dim", "count", "capacity", "_map", "ids", "matrix")

    def __init__(self, path: Path, dim: int, count: int, capacity: int, handle) -> None:
        self.path = path
        self.dim = dim
        self.count = count
        self.capacity = capacity
        self._map = handle
        ids_offset = HEADER_BYTES
        matrix_offset = ids_offset + capacity * 8
        self.ids = np.ndarray((capacity,), dtype=np.int64, buffer=handle, offset=ids_offset)
        self.matrix = np.ndarray(
            (capacity, dim), dtype=np.float32, buffer=handle, offset=matrix_offset
        )

    @classmethod
    def open(cls, path: str | Path, dim: int) -> "Pack | None":
        path = Path(path)
        try:
            with open(path, "rb") as handle:
                raw = handle.read(HEADER_BYTES)
                if len(raw) < HEADER_BYTES:
                    return None
                magic, version, stored_dim, count, capacity = HEADER.unpack(raw)
                if magic != MAGIC or version != PACK_VERSION or stored_dim != dim:
                    return None
                if count > capacity:
                    return None
                expected = HEADER_BYTES + capacity * 8 + capacity * dim * 4
                if path.stat().st_size < expected:
                    return None
            handle = open(path, "rb")
            try:
                mapping = _map_readonly(handle)
            finally:
                # The mapping owns its own handle, so the Python file object can
                # go: leaving it open leaks an fd per pack open and trips
                # ResourceWarning in tests.
                handle.close()
            return cls(path, dim, int(count), int(capacity), mapping)
        except (OSError, ValueError):
            return None

    def search(self, query: np.ndarray, limit: int) -> list[tuple[int, float]]:
        if not self.count or limit <= 0:
            return []
        matrix = self.matrix[: self.count]
        scores = matrix @ np.asarray(query, dtype=np.float32)
        take = min(limit, self.count)
        top = np.argpartition(-scores, take - 1)[:take]
        top = top[np.argsort(-scores[top])]
        ids = self.ids
        return [(int(ids[index]), float(scores[index])) for index in top]

    def close(self) -> None:
        # Dropping the ndarray views first is what makes `mmap.close()` legal on
        # Windows; otherwise it raises (BufferError for a writable map, OSError
        # for a read-only one) and the caller's cleanup is skipped.
        self.ids = None
        self.matrix = None
        try:
            self._map.close()
        except (BufferError, OSError, ValueError, AttributeError):
            pass
        self._map = None

    def stats(self) -> dict[str, int]:
        return {
            "count": self.count,
            "dim": self.dim,
            "bytes": HEADER_BYTES + self.capacity * 8 + self.capacity * self.dim * 4,
        }


def _map_readonly(handle):
    """mmap the file and keep it referenced by the ndarray, not by us.

    Closing the mmap while a numpy view still points into it raises
    BufferError, so the ndarray keeps it alive for us.
    """
    import mmap

    return mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)


def pack_path(db_path: str | Path) -> Path:
    path = Path(db_path)
    return path.with_name(path.name + ".vec")


def pack_key(conn: sqlite3.Connection) -> tuple[int, int]:
    """(rows, max id) of the chunk vectors — cheap staleness key."""
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM vectors WHERE owner_type='chunk'"
        ).fetchone()
    except sqlite3.Error:
        return (0, 0)
    return (int(row[0]), int(row[1]))


def build_pack(conn: sqlite3.Connection, db_path: str | Path, dim: int) -> Pack | None:
    """Write the pack from the `vectors` table. Returns None when there is nothing to pack."""
    target = pack_path(db_path)
    rows = conn.execute(
        "SELECT id, vec FROM vectors WHERE owner_type='chunk' ORDER BY id"
    ).fetchall()
    count = len(rows)
    if not count:
        return None
    capacity = max(count, 4096)
    ids = np.empty(capacity, dtype=np.int64)
    matrix = np.zeros((capacity, dim), dtype=np.float32)
    for position, row in enumerate(rows):
        ids[position] = int(row["id"])
        vector = np.frombuffer(row["vec"], dtype=np.float32)
        if vector.shape[0] == dim:
            matrix[position] = vector
    ids[count:] = 0

    temporary = target.with_suffix(target.suffix + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(HEADER.pack(MAGIC, PACK_VERSION, dim, count, capacity))
        ids.tofile(handle)
        matrix.tofile(handle)
    try:
        temporary.replace(target)
    except PermissionError:
        # Another process still has the pack mapped; leave theirs alone and let
        # the caller fall back to the blob path for this query.
        try:
            temporary.unlink()
        except OSError:
            pass
        return Pack.open(target, dim)
    return Pack.open(target, dim)


def drop_pack(db_path: str | Path) -> bool:
    target = pack_path(db_path)
    try:
        target.unlink()
        return True
    except OSError:
        return False
