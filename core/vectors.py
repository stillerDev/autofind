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
deleting the pack costs a rebuild, never data. Its name carries the format
version (`index.db.v4.vec`): a new version must never be asked to replace a
file an older process still has mapped — on Windows that replace fails, and a
process that quietly falls back to reading blobs answers in seconds instead of
milliseconds.
"""

from __future__ import annotations

import os
import re
import sqlite3
import struct
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

BACKEND_META = "vector_backend"
VEC_TABLE = "vec_chunks"
PACK_META = "vector_pack_key"

HEADER = struct.Struct("<8sIIQQQ")  # magic, version, dim, unique count, capacity, original count
MAGIC = b"SFVEC\x00\x00\x00"
PACK_VERSION = 4
PACK_SUFFIX = f".v{PACK_VERSION}.vec"
# `index.db.vec` (pre-versioning) and `index.db.v3.vec` are the same shape of
# name; anything else in the directory is not ours to delete.
_PACK_NAME = re.compile(r"\.(?:v\d+\.)?vec$")
# The sketch phase needs numpy's SIMD popcount (2.0+); without it the pack
# stays exact-only and every query takes the brute-force path.
SKETCH_OK = hasattr(np, "bitwise_count")
SKETCH_MIN_ROWS = 100_000  # below this, exact BLAS is already milliseconds
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
    """A memory-mapped float32 matrix with its chunk ids, deduplicated.

    Nothing is copied on open: `np.ndarray(buffer=mmap)` gives views straight
    into the mapped file, so the matrix never becomes anonymous memory and a
    second process reuses the pages the first one already faulted in.

    v4 stores only unique rows: on the real corpus 28.4% of chunk vectors are
    byte-identical (copied files, repeated boilerplate), and cosine cannot
    tell them apart. The pack keeps the mapping back — `inverse` maps every
    original row to its unique row, `orig_ids` carries each original row's
    chunk id — and search expands scores through it, so results (ids,
    multiplicities, order) are exactly what a full scan over all rows would
    return, at a 28% smaller read.

    v3's 1-bit sketches ride along on the unique rows: two-phase search
    popcounts the XOR of the query sketch (SIMD, a few MB), reranks the top
    candidates with the exact BLAS pass, and accepts the answer only when the
    last kept score beats the best *possible* excluded score by 4 sigma —
    otherwise it silently falls back to the exact full pass.
    """

    __slots__ = (
        "path", "dim", "count", "capacity", "orig_count", "_map",
        "ids", "matrix", "sketches", "inverse", "orig_ids",
        "_sketch_tries", "_sketch_accepts", "_sketch_mode",
    )

    def __init__(self, path: Path, dim: int, count: int, capacity: int,
                 orig_count: int, handle) -> None:
        self.path = path
        self.dim = dim
        self.count = count          # unique rows
        self.capacity = capacity
        self.orig_count = orig_count
        self._map = handle
        sketch_row = ((dim + 63) // 64) * 8
        ids_offset = HEADER_BYTES
        matrix_offset = ids_offset + capacity * 8
        sketch_offset = matrix_offset + capacity * dim * 4
        inverse_offset = sketch_offset + capacity * sketch_row
        orig_ids_offset = inverse_offset + orig_count * 4
        self.ids = np.ndarray((capacity,), dtype=np.int64, buffer=handle, offset=ids_offset)
        self.matrix = np.ndarray(
            (capacity, dim), dtype=np.float32, buffer=handle, offset=matrix_offset
        )
        self.sketches = np.ndarray(
            (capacity, sketch_row), dtype=np.uint8, buffer=handle, offset=sketch_offset
        )
        self.inverse = np.ndarray((orig_count,), dtype=np.uint32, buffer=handle, offset=inverse_offset)
        self.orig_ids = np.ndarray((orig_count,), dtype=np.int64, buffer=handle, offset=orig_ids_offset)
        # The two-phase sketch only pays when the exact pass it shortcuts is
        # expensive. Dedup shrinks that pass, so whether it wins depends on the
        # corpus — measured, not assumed: the first queries try it and keep
        # score; a pack whose acceptance rate lands under half reverts to exact
        # for its lifetime.
        self._sketch_tries = 0
        self._sketch_accepts = 0
        self._sketch_mode = "auto"

    @classmethod
    def open(cls, path: str | Path, dim: int) -> "Pack | None":
        path = Path(path)
        try:
            with open(path, "rb") as handle:
                raw = handle.read(HEADER_BYTES)
                if len(raw) < HEADER_BYTES:
                    return None
                magic, version, stored_dim, count, capacity, orig_count = HEADER.unpack(raw)
                if magic != MAGIC or version != PACK_VERSION or stored_dim != dim:
                    return None
                if count > capacity or orig_count < count:
                    return None
                # Blocks are written at their exact sizes; capacity is recorded
                # equal to the unique count.
                sketch_row = ((dim + 63) // 64) * 8
                expected = (HEADER_BYTES + count * 8 + count * dim * 4
                            + count * sketch_row + orig_count * 4 + orig_count * 8)
                if path.stat().st_size != expected:
                    return None
            handle = open(path, "rb")
            try:
                mapping = _map_readonly(handle)
            finally:
                # The mapping owns its own handle, so the Python file object can
                # go: leaving it open leaks an fd per pack open and trips
                # ResourceWarning in tests.
                handle.close()
            return cls(path, dim, int(count), int(capacity), int(orig_count), mapping)
        except (OSError, ValueError):
            return None

    def search(self, query: np.ndarray, limit: int) -> list[tuple[int, float]]:
        if not self.orig_count or limit <= 0:
            return []
        # Skip the copy when the caller already hands us a float32 array —
        # every embedder does, and 384 floats per query is free only if
        # nothing allocates.
        q = query if isinstance(query, np.ndarray) and query.dtype == np.float32 else np.asarray(query, dtype=np.float32)
        if (
            SKETCH_OK
            and self._sketch_mode != "off"
            and self.count >= SKETCH_MIN_ROWS
        ):
            return self._search_sketch(q, limit)
        return self._search_exact(q, limit)

    def _search_exact(self, q: np.ndarray, limit: int) -> list[tuple[int, float]]:
        scores_unique = self.matrix[: self.count] @ q
        scores = scores_unique[self.inverse]
        take = min(limit, self.orig_count)
        top = np.argpartition(-scores, take - 1)[:take]
        top = top[np.argsort(-scores[top])]
        return [(int(self.orig_ids[index]), float(scores[index])) for index in top]

    def _expand(self, scores_unique: np.ndarray, candidates: np.ndarray, limit: int):
        """Original rows for the kept unique candidates, exact top-k over them.

        Every original row mapping to a kept unique row shares its exact score,
        so the original top-k is the top-k of this expanded set — duplicates
        keep their slots, first-occurrence order breaks ties the same way a
        full scan would.
        """
        kept = np.zeros(self.count, dtype=np.bool_)
        kept[candidates] = True
        rows = np.flatnonzero(kept[self.inverse])
        scores = scores_unique[self.inverse[rows]]
        take = min(limit, len(rows))
        top = np.argpartition(-scores, take - 1)[:take]
        top = top[np.argsort(-scores[top])]
        return [(int(self.orig_ids[rows[index]]), float(scores[index])) for index in top]

    def _search_sketch(self, q: np.ndarray, limit: int) -> list[tuple[int, float]]:
        """Two-phase search with a self-checking fallback to `_search_exact`.

        The sketch is the sign pattern of the vector: for unit vectors the
        Hamming distance d between sketches estimates the angle as pi*d/dim,
        with deviation sigma = pi/2/sqrt(dim) at worst. The reranked candidates
        carry exact scores, so the only uncertainty is whether some *excluded*
        unique row could still beat the last kept one — and its best case is
        bounded by the nearest excluded sketch. If that bound cannot clear the
        kept score by 4 sigma, the answer is not trusted and the exact pass
        runs. Deduplicates cannot change the verdict: they share their
        original's score and sketch, and `_expand` restores their slots.
        """
        count = self.count
        # uint64 lanes: xor writes count*8 B instead of count*48, and the
        # popcount output sums over six lanes per row. Scratch buffers are
        # per-thread — the server searches from several threads at once, and a
        # shared scratch would race.
        row_u64 = self.sketches[:count].view(np.uint64)
        qb = np.packbits(q > 0).view(np.uint64)
        scratch = getattr(_SKETCH_SCRATCH, "buf", None)
        if scratch is None or scratch.shape != row_u64.shape:
            scratch = np.empty_like(row_u64)
            _SKETCH_SCRATCH.buf = scratch
        np.bitwise_xor(row_u64, qb, out=scratch)
        dist = np.bitwise_count(scratch).sum(axis=1, dtype=np.int32)
        keep = min(count, max(4096, 40 * limit))
        candidates = np.argpartition(dist, keep - 1)[:keep]
        scores_unique = self.matrix[:count][candidates] @ q
        take = min(limit, keep)
        top = np.argpartition(-scores_unique, take - 1)[:take]
        kept_min = float(scores_unique[top].min()) if take else 1.0
        # Best possible excluded score, from the nearest sketch we did not
        # rerank (the (keep+1)-th smallest distance; conservative on ties).
        self._sketch_tries += 1
        if keep < count:
            excluded = float(np.partition(dist, keep)[keep])
            angle = min(np.pi * excluded / self.dim, np.pi)
            sigma = np.pi / 2 / np.sqrt(self.dim)
            if kept_min - np.cos(angle) < 4 * sigma:
                self._note_sketch(False)
                return self._search_exact(q, limit)
        self._note_sketch(True)
        return self._expand(scores_unique, candidates, limit)

    def _note_sketch(self, accepted: bool) -> None:
        self._sketch_accepts += 1 if accepted else 0
        if accepted:
            return
        # Nothing accepted in the first few queries: on this corpus the bound
        # can never be cleared. The nearest excluded neighbour sits ~37 bits
        # away, which puts the cosine bound at 0.95 while the kept score is
        # ~0.17 — clearing a 4-sigma margin (0.32) from there needs a cosine
        # above 1. Every query would pay 5-9 ms of popcount for a shortcut
        # that cannot fire, so stop after three honest attempts.
        if self._sketch_tries >= 3 and self._sketch_accepts == 0:
            self._sketch_mode = "off"
        elif self._sketch_tries >= 12 and self._sketch_accepts * 2 < self._sketch_tries:
            self._sketch_mode = "off"  # measured: the shortcut loses here

    def close(self) -> None:
        # Dropping the ndarray views first is what makes `mmap.close()` legal on
        # Windows; otherwise it raises (BufferError for a writable map, OSError
        # for a read-only one) and the caller's cleanup is skipped.
        self.ids = None
        self.matrix = None
        self.sketches = None
        self.inverse = None
        self.orig_ids = None
        try:
            self._map.close()
        except (BufferError, OSError, ValueError, AttributeError):
            pass
        self._map = None

    def stats(self) -> dict[str, int]:
        sketch_row = ((self.dim + 63) // 64) * 8
        return {
            "count": self.orig_count,
            "unique": self.count,
            "dim": self.dim,
            "bytes": (HEADER_BYTES + self.capacity * 8 + self.capacity * self.dim * 4
                      + self.capacity * sketch_row + self.orig_count * 12),
        }


def _map_readonly(handle):
    """mmap the file and keep it referenced by the ndarray, not by us.

    Closing the mmap while a numpy view still points into it raises
    BufferError, so the ndarray keeps it alive for us.
    """
    import mmap

    return mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)


def pack_path(db_path: str | Path) -> Path:
    """The pack for the current format version.

    The version is in the name on purpose. Bumping `PACK_VERSION` while an
    older process still has the old file mapped used to mean the rebuild could
    not replace it (Windows refuses to replace a mapped file), so the new
    process answered every query from the blob path — 2.9 s instead of 12 ms,
    measured on the real index. A different name per version makes that
    impossible: the two versions never touch each other's file.
    """
    path = Path(db_path)
    return path.with_name(path.name + PACK_SUFFIX)


def stale_packs(db_path: str | Path) -> list[Path]:
    """Packs from other format versions (including the pre-versioned name)."""
    path = Path(db_path)
    current = pack_path(db_path)
    found: list[Path] = []
    try:
        candidates = list(path.parent.glob(path.name + "*"))
    except OSError:
        return found
    for candidate in candidates:
        if candidate == current or not _PACK_NAME.search(candidate.name):
            continue
        if candidate.is_file():
            found.append(candidate)
    return found


def _sweep(db_path: str | Path) -> None:
    """Drop dead weight: packs of other versions and abandoned build files.

    Only called after a build that landed, so the version we just wrote is the
    one being served. Every failure is ignored — a file another process still
    holds is not ours to remove, and leaving 300 MB behind is cheaper than
    breaking a live reader.
    """
    target = pack_path(db_path)
    for candidate in stale_packs(db_path):
        try:
            candidate.unlink()
        except OSError:
            pass
    try:
        leftovers = list(target.parent.glob(target.name + ".*.tmp"))
    except OSError:
        return
    for candidate in leftovers:
        try:
            candidate.unlink()
        except OSError:
            pass


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
    try:
        rows = conn.execute(
            "SELECT id, vec FROM vectors WHERE owner_type='chunk' ORDER BY id"
        ).fetchall()
    except sqlite3.Error:
        # A store without the table (a fresh database, a half-migrated one) has
        # nothing to pack. Raising here would turn every query into a 500 for a
        # file that is only a cache.
        return None
    count = len(rows)
    if not count:
        return None
    ids = np.empty(count, dtype=np.int64)
    matrix = np.zeros((count, dim), dtype=np.float32)
    for position, row in enumerate(rows):
        ids[position] = int(row["id"])
        vector = np.frombuffer(row["vec"], dtype=np.float32)
        if vector.shape[0] == dim:
            matrix[position] = vector

    # Byte-identical rows are indistinguishable to cosine: store each pattern
    # once and keep the original-row -> unique-row map for expansion.
    flat = np.ascontiguousarray(matrix).view(np.void(dim * 4))
    _, first_idx, inverse = np.unique(flat, return_index=True, return_inverse=True)
    order = np.argsort(first_idx)  # unique rows in ascending original order
    positions = first_idx[order]
    unique_count = len(positions)
    rank = np.empty(unique_count, dtype=np.uint32)
    rank[order] = np.arange(unique_count, dtype=np.uint32)
    inverse = rank[inverse.astype(np.uint32)].astype(np.uint32)

    unique_ids = ids[positions]
    unique_matrix = np.ascontiguousarray(matrix[positions])
    sketch_row = ((dim + 63) // 64) * 8
    sketches = np.zeros((unique_count, sketch_row), dtype=np.uint8)
    sketches[:] = np.packbits(unique_matrix > 0, axis=1)

    # A unique temp name per build: a pack we kept serving from an earlier
    # failed replace is still mapped, and Windows will not let us open a mapped
    # file for writing. Same directory, because the final step is a rename.
    handle, temporary_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=str(target.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as out:
            out.write(HEADER.pack(MAGIC, PACK_VERSION, dim, unique_count, unique_count, count))
            unique_ids.tofile(out)
            unique_matrix.tofile(out)
            sketches.tofile(out)
            inverse.tofile(out)
            ids.tofile(out)  # orig_ids, original row order

        for attempt in range(3):
            try:
                temporary.replace(target)
                break
            except PermissionError:
                # Another process still has this version mapped (it rebuilt
                # from a changed index at the same moment). Retrying gives it
                # a moment to let go.
                time.sleep(0.05 * (attempt + 1))
        else:
            # Still locked. Serve the freshly built file from its temp name
            # instead of returning None: None demotes every query to the blob
            # path, which re-reads all vectors from SQLite per query — 2.9 s
            # against 12 ms. A file with an ugly name is the cheaper failure.
            # `_sweep` removes it once a later build lands properly.
            return Pack.open(temporary, dim)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    _sweep(db_path)
    return Pack.open(target, dim)


def drop_pack(db_path: str | Path) -> bool:
    removed = False
    for candidate in [pack_path(db_path), *stale_packs(db_path)]:
        try:
            candidate.unlink()
            removed = True
        except OSError:
            pass
    return removed


class _PackEntry:
    __slots__ = ("stamps", "pack", "uses")

    def __init__(self, stamps: tuple, pack: "Pack") -> None:
        self.stamps = stamps
        self.pack = pack
        self.uses = 0


class PackCache:
    """One open pack per database, shared by every Store in the process.

    The UI opens a short-lived Store per request. Without this cache each
    request paid for a `SELECT COUNT(*)` over the vectors table — a 237k-row
    scan, ~15 ms — just to decide whether the pack was fresh, plus an mmap
    teardown in `Store.close()`, which on Windows costs another ~55 ms. Both
    numbers came out of a cProfile of the live server; together they were more
    than half of a 110 ms query.

    Freshness is decided by stat stamps of the database, its WAL and the pack
    file — the same signal `NameIndex` uses — so a warm query costs two stats
    and a dict lookup. Only on a stamp change does the (rows, max id) key get
    counted and the pack reopened or rebuilt.

    Packs are checked out: an `acquire`d pack is never closed or swapped while
    its caller holds it, so a search in flight can trust its mapping. An
    evicted-but-checked-out pack moves to a zombie list and closes when the
    last holder releases it. Every `acquire` must be balanced by a `release`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, _PackEntry] = {}
        self._zombies: dict[str, list[_PackEntry]] = {}
        self._refs: dict[str, int] = {}  # live Stores per database

    def ref(self, db_path: str | Path) -> None:
        """A Store on this database is now open.

        The last one to close releases the mapping, which is what lets tests
        delete their temporary directories: a pack nobody holds must not keep
        the file busy. A server keeps one resident reader open, so in real use
        the pack stays mapped between requests.
        """
        with self._lock:
            self._refs[str(pack_path(db_path))] = self._refs.get(str(pack_path(db_path)), 0) + 1

    def unref(self, db_path: str | Path) -> None:
        key = str(pack_path(db_path))
        with self._lock:
            count = self._refs.get(key, 0) - 1
            if count > 0:
                self._refs[key] = count
                return
            self._refs.pop(key, None)
            self._retire_locked(key)

    @staticmethod
    def _stamps(db_path: str | Path) -> tuple:
        pack_file = pack_path(db_path)
        stamps = []
        for candidate in (Path(db_path), Path(str(db_path) + "-wal"), pack_file):
            try:
                info = candidate.stat()
                stamps.append((info.st_mtime_ns, info.st_size))
            except OSError:
                stamps.append((0, 0))
        return tuple(stamps)

    def _retire_locked(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry is None:
            return
        if entry.uses > 0:
            self._zombies.setdefault(key, []).append(entry)
        else:
            entry.pack.close()

    def acquire(self, db_path: str | Path, dim: int, conn: sqlite3.Connection):
        """Return the pack checked out to this caller, or None when empty."""
        key = str(pack_path(db_path))
        stamps = self._stamps(db_path)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.stamps == stamps:
                entry.uses += 1
                return entry.pack
        # Stale or unseen: revalidate against the table itself. Once per
        # change, not once per query.
        table_key = pack_key(conn)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.stamps == stamps:
                entry.uses += 1  # another thread refreshed while we counted
                return entry.pack
            if not table_key:
                self._retire_locked(key)
                return None
            pack = Pack.open(key, dim)
            # The key must match over the ORIGINAL rows: the max-id chunk can
            # itself be a duplicate (its vector copies an earlier row), so only
            # orig_ids — which carries every row — is guaranteed to hold it.
            if pack is None or (pack.orig_count, int(pack.orig_ids.max())) != table_key:
                if pack is not None:
                    pack.close()
                # The old mapping has to go before the file is rewritten.
                self._retire_locked(key)
                pack = build_pack(conn, db_path, dim)
            else:
                self._retire_locked(key)
            if pack is None:
                return None
            entry = _PackEntry(self._stamps(db_path), pack)
            entry.uses = 1
            self._entries[key] = entry
            return pack

    def release(self, db_path: str | Path, pack: "Pack") -> None:
        key = str(pack_path(db_path))
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.pack is pack:
                entry.uses -= 1
                return
            zombies = self._zombies.get(key)
            if zombies:
                for zombie in zombies:
                    if zombie.pack is pack:
                        zombie.uses -= 1
                        if zombie.uses <= 0:
                            zombies.remove(zombie)
                            zombie.pack.close()
                        return

    def put(self, db_path: str | Path, pack) -> None:
        """Publish a freshly built pack (from `rebuild_pack`). Unchecked: a
        caller that wants to hold it must go through `acquire` after."""
        if pack is None:
            return
        key = str(pack_path(db_path))
        with self._lock:
            self._retire_locked(key)
            self._entries[key] = _PackEntry(self._stamps(db_path), pack)

    def build(self, db_path: str | Path, conn: sqlite3.Connection, dim: int):
        """Rebuild the pack from the `vectors` table and publish it.

        The old entry is retired before the write so Windows is not asked to
        replace a file that is still mapped (a concurrent checked-out reader
        keeps the old mapping alive in the zombie list until it releases).
        """
        with self._lock:
            self._retire_locked(str(pack_path(db_path)))
        pack = build_pack(conn, db_path, dim)
        self.put(db_path, pack)
        return pack

    def drop(self, db_path: str | Path) -> None:
        with self._lock:
            self._retire_locked(str(pack_path(db_path)))


PACKS = PackCache()
_SKETCH_SCRATCH = threading.local()
