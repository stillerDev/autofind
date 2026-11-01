"""SQLite storage: metadata, FTS5 full text, image records and vector blobs.

Everything lives in one file in WAL mode. Vectors are raw float32 blobs
searched by brute force in numpy: at the 50k-chunk target that is a 77 MB
matrix and a few milliseconds per query, so an ANN index would be premature.
sqlite-vec can be swapped in behind `knn()` / `knn_images()` later without
touching the callers.
"""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id        INTEGER PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    size      INTEGER NOT NULL,
    mtime     REAL NOT NULL,
    sha256    TEXT NOT NULL,
    kind      TEXT NOT NULL,
    indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(text, tokenize="unicode61 remove_diacritics 2");
CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY,
    path       TEXT UNIQUE NOT NULL,
    file_id    INTEGER,
    sha256     TEXT,
    width      INTEGER,
    height     INTEGER,
    size       INTEGER,
    mtime      REAL,
    thumb      BLOB,
    ocr_text   TEXT,
    ocr_engine TEXT,
    indexed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_images_sha ON images(sha256);
CREATE INDEX IF NOT EXISTS idx_images_file ON images(file_id);
CREATE TABLE IF NOT EXISTS vectors (
    id         INTEGER PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_id   INTEGER NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_owner ON vectors(owner_type, owner_id);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Frecency: how often a file was opened and when. One row per file, written on
-- /api/open; ranking reads it as a bonus, so a file you actually use floats up
-- without ever outranking an exact name match.
CREATE TABLE IF NOT EXISTS usage (
    file_id   INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    opens     INTEGER NOT NULL DEFAULT 0,
    last_open REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON files(mtime);
CREATE INDEX IF NOT EXISTS idx_files_size ON files(size);
"""

# Columns added after the first release. `files.name` and `files.char_mask` are
# denormalised on purpose: fuzzy name matching scans every file, and computing
# a basename and a 64-bit bloom per row in Python for 100k files costs more than
# storing 20 bytes once.
FILE_COLUMNS = {
    "name": "TEXT",
    "char_mask": "INTEGER DEFAULT 0",
}

IMAGE_COLUMNS = {
    "file_id": "INTEGER",
    "width": "INTEGER",
    "height": "INTEGER",
    "size": "INTEGER",
    "mtime": "REAL",
    "ocr_text": "TEXT",
    "ocr_engine": "TEXT",
}


def char_mask(text: str) -> int:
    """64-bit character-presence bloom for the fuzzy prefilter.

    A candidate can only match a subsequence query if it contains every
    character of that query, so one AND per file rejects most of the corpus
    before the expensive matcher runs. Collisions are harmless: the mask only
    produces false positives, never false negatives.
    """
    mask = 0
    for ch in text.lower():
        if "a" <= ch <= "z":
            bit = ord(ch) - 97
        elif "0" <= ch <= "9":
            bit = 26 + ord(ch) - 48
        elif ch == "_":
            bit = 36
        elif ch == "-":
            bit = 37
        elif ch == ".":
            bit = 38
        elif ch in "/\\":
            bit = 39
        elif ch == " ":
            bit = 40
        else:
            bit = 41 + (ord(ch) % 21)
        # 63 bits, not 64: SQLite integers are signed, so bit 63 would turn the
        # value into a REAL and break the bitwise AND.
        mask |= 1 << (bit % 63)
    return mask


def basename(path: str) -> str:
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def _regexp(pattern: str, value: str | None) -> bool:
    if value is None:
        return False
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


SCHEMA_VERSION = 2


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> bool:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    added = False
    for column, sql_type in columns.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
            added = True
    return added


def _migrate_connection(conn: sqlite3.Connection) -> None:
    """Bring any index up to the current schema. Safe to run repeatedly.

    Indexes built before `name`/`char_mask` existed need one pass over them:
    eight thousand rows take well under a second, a hundred thousand a few
    seconds, once, on the first open after the upgrade.
    """
    _add_columns(conn, "images", IMAGE_COLUMNS)
    if _add_columns(conn, "files", FILE_COLUMNS):
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        conn.executemany(
            "UPDATE files SET name = ?, char_mask = ? WHERE id = ?",
            [(basename(row["path"]), char_mask(basename(row["path"])), row["id"]) for row in rows],
        )
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


class Store:
    def __init__(
        self,
        path: str | Path,
        embedder: str = "hashing-384",
        dim: int = 384,
        readonly: bool = False,
        cache: "VectorCache | None" = None,
    ) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self.cache = cache
        if readonly:
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("regexp", 2, _regexp, deterministic=True)
        self._vec_ids: np.ndarray | None = None
        self._vec_matrix: np.ndarray | None = None
        self._vec_key: tuple[int, int] | None = None
        self._file_key: tuple[int, int] | None = None
        if not readonly:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self._check_embedder(embedder, dim)
        elif self._schema_version() < SCHEMA_VERSION:
            # A reader that opens an index built by an older version still has
            # to migrate it, otherwise every `usage` or `char_mask` query fails.
            # It costs one short writable connection, once, after an upgrade.
            self._migrate_externally()
        self.conn.commit()

    # ---------------------------------------------------------------- schema

    def _schema_version(self) -> int:
        try:
            return int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.Error:
            return 0

    def _migrate_externally(self) -> None:
        conn = None
        try:
            conn = sqlite3.connect(str(self.path))
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            _migrate_connection(conn)
            conn.commit()
        except sqlite3.Error:
            pass  # genuinely read-only file: the tolerant queries below cope
        finally:
            if conn is not None:
                conn.close()

    def _migrate(self) -> None:
        _migrate_connection(self.conn)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _check_embedder(self, embedder: str, dim: int) -> None:
        stored = self.get_meta("embedder")
        if stored is None:
            self.set_meta("embedder", embedder)
            self.set_meta("dim", str(dim))
        elif stored != embedder:
            raise RuntimeError(
                f"index built with embedder '{stored}', current is '{embedder}'. "
                "Reindex into a fresh database or pass --db."
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    # ----------------------------------------------------------------- files

    def get_file(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()

    def file_by_id(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    def upsert_file(self, path: str, size: int, mtime: float, sha256: str, kind: str) -> int:
        name = basename(path)
        self.conn.execute(
            "INSERT INTO files(path, name, char_mask, size, mtime, sha256, kind, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET name=excluded.name, char_mask=excluded.char_mask, "
            "size=excluded.size, mtime=excluded.mtime, sha256=excluded.sha256, "
            "kind=excluded.kind, indexed_at=excluded.indexed_at",
            (path, name, char_mask(name), size, mtime, sha256, kind, time.time()),
        )
        row = self.conn.execute("SELECT id FROM files WHERE path = ?", (path,)).fetchone()
        return int(row["id"])

    def replace_chunks(self, file_id: int, chunks: Sequence[str], vectors: np.ndarray | None) -> int:
        self._drop_chunks_of(file_id)
        for order, text in enumerate(chunks):
            cursor = self.conn.execute(
                "INSERT INTO chunks(file_id, ord, text) VALUES(?,?,?)", (file_id, order, text)
            )
            chunk_id = int(cursor.lastrowid)
            self.conn.execute("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", (chunk_id, text))
            if vectors is not None and len(vectors) > order:
                self.conn.execute(
                    "INSERT INTO vectors(owner_type, owner_id, dim, vec) VALUES('chunk',?,?,?)",
                    (chunk_id, int(vectors.shape[1]), vectors[order].astype(np.float32).tobytes()),
                )
        self._invalidate_vectors()
        return len(chunks)

    def _drop_chunks_of(self, file_id: int) -> None:
        old_ids = [row["id"] for row in self.conn.execute(
            "SELECT id FROM chunks WHERE file_id = ?", (file_id,)
        )]
        if not old_ids:
            return
        placeholders = ",".join("?" * len(old_ids))
        self.conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in old_ids])
        self.conn.execute(
            f"DELETE FROM vectors WHERE owner_type='chunk' AND owner_id IN ({placeholders})", old_ids
        )
        self.conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))

    def drop_file(self, file_id: int) -> None:
        self._drop_chunks_of(file_id)
        image = self.conn.execute("SELECT id FROM images WHERE file_id = ?", (file_id,)).fetchone()
        if image:
            self.conn.execute(
                "DELETE FROM vectors WHERE owner_type='image' AND owner_id = ?", (int(image["id"]),)
            )
            self.conn.execute("DELETE FROM images WHERE id = ?", (int(image["id"]),))
        self.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        self._invalidate_vectors()

    def prune(self, known_paths: set[str]) -> int:
        removed = 0
        for row in self.conn.execute("SELECT id, path FROM files").fetchall():
            if row["path"] not in known_paths:
                self.drop_file(int(row["id"]))
                removed += 1
        return removed

    def commit(self) -> None:
        self.conn.commit()

    # ---------------------------------------------------------------- images

    def image_by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM images WHERE sha256 = ?", (sha256,)).fetchone()

    def image_by_file_id(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM images WHERE file_id = ?", (file_id,)).fetchone()

    def upsert_image(
        self,
        path: str,
        file_id: int,
        sha256: str,
        width: int,
        height: int,
        size: int,
        mtime: float,
        thumb: bytes | None,
        ocr_text: str | None,
        ocr_engine: str | None,
        vector: np.ndarray | None,
    ) -> int:
        self.conn.execute(
            "INSERT INTO images(path, file_id, sha256, width, height, size, mtime, thumb, ocr_text, ocr_engine, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET file_id=excluded.file_id, sha256=excluded.sha256, "
            "width=excluded.width, height=excluded.height, size=excluded.size, mtime=excluded.mtime, "
            "thumb=excluded.thumb, ocr_text=excluded.ocr_text, ocr_engine=excluded.ocr_engine, "
            "indexed_at=excluded.indexed_at",
            (path, file_id, sha256, width, height, size, mtime, thumb, ocr_text, ocr_engine, time.time()),
        )
        row = self.conn.execute("SELECT id FROM images WHERE path = ?", (path,)).fetchone()
        image_id = int(row["id"])
        self.conn.execute("DELETE FROM vectors WHERE owner_type='image' AND owner_id = ?", (image_id,))
        if vector is not None:
            self.conn.execute(
                "INSERT INTO vectors(owner_type, owner_id, dim, vec) VALUES('image',?,?,?)",
                (image_id, int(vector.shape[0]), vector.astype(np.float32).tobytes()),
            )
        self._invalidate_vectors()
        return image_id

    def thumb(self, file_id: int) -> bytes | None:
        row = self.conn.execute("SELECT thumb FROM images WHERE file_id = ?", (file_id,)).fetchone()
        return bytes(row["thumb"]) if row and row["thumb"] else None

    def image_rows(self, limit: int = 200, offset: int = 0) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT i.id, i.path, i.width, i.height, i.ocr_text, i.file_id, f.mtime "
            "FROM images i LEFT JOIN files f ON f.id = i.file_id ORDER BY f.mtime DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    # --------------------------------------------------------------- search

    def bm25(self, match_query: str, limit: int) -> list[tuple[int, float]]:
        try:
            rows = self.conn.execute(
                "SELECT rowid AS id, bm25(chunks_fts) AS score FROM chunks_fts "
                "WHERE chunks_fts MATCH ? ORDER BY score LIMIT ?",
                (match_query, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [(int(row["id"]), float(row["score"])) for row in rows]

    def vector_index(self) -> tuple[np.ndarray, np.ndarray]:
        if self.cache is not None:
            return self.cache.get(self)
        return self._load_vectors()

    def _load_vectors(self) -> tuple[np.ndarray, np.ndarray]:
        key_row = self.conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM vectors WHERE owner_type='chunk'"
        ).fetchone()
        key = (int(key_row["n"]), int(key_row["m"]))
        if self._vec_key == key and self._vec_matrix is not None and self._vec_ids is not None:
            return self._vec_ids, self._vec_matrix

        rows = self.conn.execute(
            "SELECT id, vec FROM vectors WHERE owner_type='chunk' ORDER BY id"
        ).fetchall()
        if not rows:
            self._vec_ids = np.zeros(0, dtype=np.int64)
            self._vec_matrix = np.zeros((0, 384), dtype=np.float32)
        else:
            self._vec_ids = np.array([row["id"] for row in rows], dtype=np.int64)
            self._vec_matrix = np.vstack(
                [np.frombuffer(row["vec"], dtype=np.float32) for row in rows]
            ).astype(np.float32)
        self._vec_key = key
        return self._vec_ids, self._vec_matrix

    def knn(self, query_vector: np.ndarray, limit: int) -> list[tuple[int, float]]:
        return _top_k(*self.vector_index(), query_vector, limit)

    def image_index(self, dim: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        sql = "SELECT owner_id AS id, vec FROM vectors WHERE owner_type='image'"
        params: tuple = ()
        if dim is not None:
            sql += " AND dim = ?"
            params = (dim,)
        rows = self.conn.execute(sql + " ORDER BY owner_id", params).fetchall()
        if not rows:
            return np.zeros(0, dtype=np.int64), np.zeros((0, dim or 1), dtype=np.float32)
        ids = np.array([row["id"] for row in rows], dtype=np.int64)
        matrix = np.vstack([np.frombuffer(row["vec"], dtype=np.float32) for row in rows]).astype(np.float32)
        return ids, matrix

    def knn_images(self, query_vector: np.ndarray, limit: int) -> list[tuple[int, float]]:
        ids, matrix = self.image_index(dim=int(query_vector.shape[0]))
        return _top_k(ids, matrix, query_vector, limit)

    def chunk_rows(self, ids: Iterable[int]) -> dict[int, sqlite3.Row]:
        ids = list(ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT c.id, c.ord, c.text, f.id AS file_id, f.path, f.kind, f.size, f.mtime "
            f"FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.id IN ({placeholders})",
            ids,
        ).fetchall()
        return {int(row["id"]): row for row in rows}

    # ------------------------------------------------------- fuzzy / listing

    def fuzzy_candidates(self, mask: int, limit: int) -> list[sqlite3.Row]:
        """Files whose name could contain every character of the query.

        One bitwise AND per row; a full scan of 100k integer columns is a few
        milliseconds, which is cheaper than maintaining a separate name index.
        """
        try:
            return self.conn.execute(
                "SELECT id, path, name, kind, size, mtime FROM files "
                "WHERE (char_mask & ?) = ? ORDER BY mtime DESC LIMIT ?",
                (mask, mask, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    def all_files(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, path, name, kind, size, mtime FROM files"
        ).fetchall()

    def file_rows_by_ids(self, ids: Iterable[int]) -> list[sqlite3.Row]:
        ids = list(ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return self.conn.execute(
            f"SELECT id, path, name, kind, size, mtime FROM files WHERE id IN ({placeholders})",
            ids,
        ).fetchall()

    def regex_chunks(self, pattern: str, limit: int) -> list[sqlite3.Row]:
        """Bounded regex scan over chunk text.

        SQLite has no REGEXP operator; the callback is registered in __init__.
        This is a full scan by design — it only runs for an explicit `re:` query,
        and the limit bounds the damage on a large index.
        """
        try:
            return self.conn.execute(
                "SELECT c.id, c.ord, c.text, f.id AS file_id, f.path, f.kind, f.size, f.mtime "
                "FROM chunks c JOIN files f ON f.id = c.file_id "
                "WHERE c.text REGEXP ? LIMIT ?",
                (pattern, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            return []

    # ---------------------------------------------------------------- usage

    def record_open(self, file_id: int) -> None:
        self.conn.execute(
            "INSERT INTO usage(file_id, opens, last_open) VALUES(?, 1, ?) "
            "ON CONFLICT(file_id) DO UPDATE SET opens = opens + 1, last_open = excluded.last_open",
            (file_id, time.time()),
        )

    def usage_map(self) -> dict[int, tuple[int, float]]:
        try:
            rows = self.conn.execute("SELECT file_id, opens, last_open FROM usage").fetchall()
        except sqlite3.OperationalError:
            return {}
        return {int(row["file_id"]): (int(row["opens"]), float(row["last_open"])) for row in rows}

    def stats(self) -> dict[str, int]:
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        return {
            "files": scalar("SELECT COUNT(*) FROM files"),
            "chunks": scalar("SELECT COUNT(*) FROM chunks"),
            "vectors": scalar("SELECT COUNT(*) FROM vectors WHERE owner_type='chunk'"),
            "images": scalar("SELECT COUNT(*) FROM images"),
            "ocr_images": scalar("SELECT COUNT(*) FROM images WHERE ocr_text IS NOT NULL AND ocr_text != ''"),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
        }

    def _invalidate_vectors(self) -> None:
        self._vec_key = None
        self._vec_ids = None
        self._vec_matrix = None

    def touch_check(self) -> None:
        """Drop the vector cache when another connection wrote to the file.

        A long-lived reader (the UI server) would otherwise keep serving the
        matrix it loaded at startup. WAL writes land in -wal first, so the main
        file's mtime alone is not enough.
        """
        stamps = []
        for candidate in (self.path, Path(str(self.path) + "-wal")):
            try:
                stamps.append(candidate.stat().st_mtime_ns)
            except OSError:
                stamps.append(0)
        key = (stamps[0], stamps[1])
        if key != self._file_key:
            self._file_key = key
            self._invalidate_vectors()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.commit()
        self.close()


def _file_stamps(path: Path) -> tuple[int, int]:
    stamps = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            stamps.append(candidate.stat().st_mtime_ns)
        except OSError:
            stamps.append(0)
    return (stamps[0], stamps[1])


class VectorCache:
    """Process-wide cache of the chunk vector matrix.

    The UI opens a short-lived Store per request. Without this cache every
    request re-reads 200k blobs and rebuilds a 300 MB matrix, which measured at
    ~2.6 s per query. One read-only matrix, shared by all threads, invalidated
    when the database file (or its WAL) changes.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._ids: np.ndarray | None = None
        self._matrix: np.ndarray | None = None

    def get(self, store: "Store") -> tuple[np.ndarray, np.ndarray]:
        key = (str(store.path), _file_stamps(store.path))
        with self._lock:
            if key == self._key and self._ids is not None and self._matrix is not None:
                return self._ids, self._matrix

        ids, matrix = store._load_vectors()
        matrix.setflags(write=False)
        ids.setflags(write=False)
        with self._lock:
            self._key, self._ids, self._matrix = key, ids, matrix
        return ids, matrix

    def clear(self) -> None:
        with self._lock:
            self._key = None
            self._ids = None
            self._matrix = None


def _top_k(ids: np.ndarray, matrix: np.ndarray, query: np.ndarray, limit: int) -> list[tuple[int, float]]:
    if not len(ids) or matrix.size == 0:
        return []
    if matrix.shape[1] != query.shape[0]:
        return []
    scores = matrix @ query.astype(np.float32)
    take = min(limit, len(ids))
    top = np.argpartition(-scores, take - 1)[:take]
    top = top[np.argsort(-scores[top])]
    return [(int(ids[i]), float(scores[i])) for i in top]
