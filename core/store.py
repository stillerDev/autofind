"""SQLite storage: metadata, FTS5 full text, image records and file vectors.

Everything lives in one file in WAL mode. Since schema v9 the text of a
document is stored **once** — a zlib-compressed blob in `file_texts`, shared by
every file with the same content — and a chunk is only a span into it
`(file_id, ord, start, len)`. That removed the second copy of the corpus that
the chunk table used to carry (~322 MB here) and let the full-text index be
contentless, so it stores postings and nothing else.

Vectors are one binary row per file (`file_vectors`, 48 bytes at 384 dims) and
are searched by brute force in numpy: 14 000 rows is a 5 MB matvec, cheaper
than any index would be. See `core.filevec`.
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sqlite3
import threading
import time
import zlib
from array import array
from bisect import bisect_right
from functools import lru_cache
from itertools import groupby
from pathlib import Path
from typing import Callable, Iterable, Iterator, Sequence

from .lazynp import np

from . import vectors as vecstore
from . import vault as vault_mod
from . import bm25 as bm25mod
from . import filevec
from .chunker import chunk_spans, normalize

_BM25: dict = {"key": None, "index": None, "building": None}
_BM25_LOCK = threading.Lock()

# One packed-bits array per database, shared process-wide: 4.8 MB at 100k
# files. `knn` scores these rows through per-byte lookup tables, so the ±1
# float32 matrix (154 MB at 100k files) never needs to exist in RAM.
_FILEVEC_CACHE: dict = {"key": None, "ids": None, "bits": None}
_FILEVEC_LOCK = threading.Lock()

# The in-RAM BM25 is a trade, not a free win: on a 298k-chunk index it took
# 35-37 s to build and ~1.5 GB, and its query time was a wash against FTS5
# (faster on rare multi-word queries, slower on common single words) while the
# build thread stole the GIL from the first query. Long-lived surfaces (the app
# server, the MCP server) keep it; one-shot CLI searches pass `ram_bm25=False`
# so a single query never starts a 35 s build the process will not outlive.
# Set AUTOFIND_RAM_BM25=0 to turn it off everywhere.
_RAM_BM25_ENABLED = os.environ.get("AUTOFIND_RAM_BM25", "0").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
from .cjk import cjk_bigrams

SCHEMA = """
-- The directory part of a path is stored once per distinct prefix: a corpus
-- of 100k files in 100 directories wrote every folder name 1000 times over.
CREATE TABLE IF NOT EXISTS dirs (
    dir_id INTEGER PRIMARY KEY,
    path   TEXT NOT NULL UNIQUE
);
-- The file row itself: basename + dir reference. Content hashes are 32-byte
-- BLOBs, not 64-character hex strings - the hash is data, not prose.
CREATE TABLE IF NOT EXISTS file_rows (
    id         INTEGER PRIMARY KEY,
    dir_id     INTEGER NOT NULL REFERENCES dirs(dir_id),
    name       TEXT NOT NULL,
    char_mask  INTEGER,
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    sha256     BLOB NOT NULL,
    kind       TEXT NOT NULL,
    doc_date   TEXT,
    indexed_at REAL NOT NULL,
    text_hash  BLOB,
    UNIQUE(dir_id, name)
);
CREATE INDEX IF NOT EXISTS idx_files_text_hash ON file_rows(text_hash);
CREATE INDEX IF NOT EXISTS idx_files_mtime ON file_rows(mtime);
CREATE INDEX IF NOT EXISTS idx_files_size ON file_rows(size);
-- The `files` view and its INSTEAD OF triggers are created by
-- _migrate_v12, which is the only place that knows whether `files` is
-- still a legacy table or already the view.

CREATE TABLE IF NOT EXISTS file_texts (
    hash       BLOB PRIMARY KEY,
    raw_len    INTEGER NOT NULL,
    block_size INTEGER NOT NULL DEFAULT 0,
    offsets    BLOB NOT NULL DEFAULT x'',
    data       BLOB NOT NULL
);
-- A chunk is a span into its file's text, never a copy of it. The text lives
-- once per *content*: two byte-identical documents share one `file_texts` row.
CREATE TABLE IF NOT EXISTS chunks (
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES file_rows(id) ON DELETE CASCADE,
    ord     INTEGER NOT NULL,
    start   INTEGER NOT NULL,
    len     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);
-- One vector per file, one bit per dimension: 48 bytes at 384 dims. The sign
-- pattern is what the KNN scan multiplies against a float query vector.
CREATE TABLE IF NOT EXISTS file_vectors (
    file_id INTEGER PRIMARY KEY REFERENCES file_rows(id) ON DELETE CASCADE,
    dim     INTEGER NOT NULL,
    bits    BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS images (
    id         INTEGER PRIMARY KEY,
    path       TEXT UNIQUE NOT NULL,
    file_id    INTEGER,
    sha256     BLOB,
    width      INTEGER,
    height     INTEGER,
    size       INTEGER,
    mtime      REAL,
    thumb      BLOB,
    ocr_text   TEXT,
    ocr_engine TEXT,
    phash      INTEGER,
    indexed_at REAL
);
CREATE INDEX IF NOT EXISTS idx_images_sha ON images(sha256);
CREATE INDEX IF NOT EXISTS idx_images_phash ON images(phash);
CREATE INDEX IF NOT EXISTS idx_images_file ON images(file_id);
CREATE TABLE IF NOT EXISTS vectors (
    id         INTEGER PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_id   INTEGER NOT NULL,
    dim        INTEGER NOT NULL,
    vec        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vectors_owner ON vectors(owner_type, owner_id);
CREATE TABLE IF NOT EXISTS recent_searches (
    q TEXT NOT NULL,
    at REAL NOT NULL
);
    CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
-- Frecency: how often a file was opened and when. One row per file, written on
-- /api/open; ranking reads it as a bonus, so a file you actually use floats up
-- without ever outranking an exact name match.
CREATE TABLE IF NOT EXISTS usage (
    file_id   INTEGER PRIMARY KEY REFERENCES file_rows(id) ON DELETE CASCADE,
    opens     INTEGER NOT NULL DEFAULT 0,
    last_open REAL NOT NULL DEFAULT 0
);

-- The personal layer: what makes this install *yours*. kensaku keeps pinned
-- files, saved searches and a personal dictionary; these are the same three
-- things, in the same file as the index, so they survive a reinstall of the
-- application and never leave the machine.
CREATE TABLE IF NOT EXISTS pins (
    file_id  INTEGER PRIMARY KEY REFERENCES file_rows(id) ON DELETE CASCADE,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS saved (
    id        INTEGER PRIMARY KEY,
    query     TEXT NOT NULL,
    label     TEXT NOT NULL DEFAULT '',
    created   REAL NOT NULL,
    used      INTEGER NOT NULL DEFAULT 0,
    last_used REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_saved_query ON saved(query);
CREATE TABLE IF NOT EXISTS aliases (
    alias  TEXT PRIMARY KEY,
    expand TEXT NOT NULL
);
-- Tags are the user's own words about a file ("invoice", "tax 2025"). Any
-- number per file; the filters are an AND so two tags narrow, not widen.
CREATE TABLE IF NOT EXISTS tags (
    file_id  INTEGER NOT NULL REFERENCES file_rows(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (file_id, tag)
);
CREATE INDEX IF NOT EXISTS idx_tags_tag ON tags(tag);
-- A note is a sentence a human wrote about a file. kensaku has no answer to
-- "why did I keep this?" — this is ours, stored beside the index like the
-- rest of the personal layer.
CREATE TABLE IF NOT EXISTS notes (
    file_id  INTEGER PRIMARY KEY REFERENCES file_rows(id) ON DELETE CASCADE,
    text     TEXT NOT NULL,
    added_at REAL NOT NULL
);
-- A tag can wear a colour: the row's dot in the UI and the drawer chip use
-- it, and the colour travels with the tag, not the file.
CREATE TABLE IF NOT EXISTS tag_colors (
    tag    TEXT PRIMARY KEY,
    color  TEXT NOT NULL
);
-- A collection is a named set of files, like a playlist: "Project X", "To
-- read". Membership is many-to-many; the order is the moment a file joined.
CREATE TABLE IF NOT EXISTS collections (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL UNIQUE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_files (
    collection_id INTEGER NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
    file_id       INTEGER NOT NULL REFERENCES file_rows(id) ON DELETE CASCADE,
    added_at      REAL NOT NULL,
    PRIMARY KEY (collection_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_collection_files_file ON collection_files(file_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_collections_name
    ON collections(name COLLATE NOCASE);
-- Code definitions, extracted at index time. kensaku parses with tree-sitter
-- and finds functions, classes and structs; this is the same feature without a
-- parser dependency: a per-language pattern table that only fires on lines that
-- look like a definition.
CREATE TABLE IF NOT EXISTS symbols (
    id      INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES file_rows(id) ON DELETE CASCADE,
    name    TEXT NOT NULL,
    lowered TEXT NOT NULL,
    kind    TEXT NOT NULL,
    line    INTEGER NOT NULL,
    text    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(lowered);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_id);
"""

# Columns added after the first release. `files.name` and `files.char_mask` are
# denormalised on purpose: fuzzy name matching scans every file, and computing
# a basename and a 64-bit bloom per row in Python for 100k files costs more than
# storing 20 bytes once.
FILE_COLUMNS = {
    "name": "TEXT",
    "char_mask": "INTEGER DEFAULT 0",
    # The date the document itself claims (a receipt's printed date), ISO or
    # NULL until a re-index reads one. mtime is when the file landed on disk.
    "doc_date": "TEXT",
}

# The v9 pointer from a file to its (shared, compressed) text. Kept out of
# FILE_COLUMNS so adding it does not trigger the name/char_mask backfill.
TEXT_COLUMNS = {"text_hash": "TEXT"}
# Block compression metadata for `file_texts` (see `_pack_text`); older rows
# have `block_size = 0` and are repacked once on open.
TEXT_BLOCK_COLUMNS = {"block_size": "INTEGER NOT NULL DEFAULT 0",
                      "offsets": "BLOB NOT NULL DEFAULT x''"}

IMAGE_COLUMNS = {
    "file_id": "INTEGER",
    "width": "INTEGER",
    "height": "INTEGER",
    "size": "INTEGER",
    "mtime": "REAL",
    "ocr_text": "TEXT",
    "ocr_boxes": "TEXT",
    "ocr_engine": "TEXT",
    # 64-bit dHash, written at index time: screenshot bursts are collapsed by
    # comparing it against neighbours in the same folder.
    "phash": "INTEGER",
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


def _split_path(path: str) -> tuple[str, str]:
    """(directory prefix, basename) of an absolute path.

    The directory keeps no trailing separator, so `dirs.path || '\\' || name`
    reproduces the original string byte-for-byte; both separators are honoured
    because imports may hand over forward slashes."""
    text = str(path)
    cut = max(text.rfind("\\"), text.rfind("/"))
    if cut < 0:
        return "", text
    return text[:cut], text[cut + 1:]


def _best_copy(items: Sequence[dict], prefer_size: bool = False) -> dict:
    """The copy to keep in a duplicate group.

    A path in a temp folder or a download folder is the copy that was made by
    accident; a shallow path is easier to find; the newest copy is the one that
    was edited last. For documents the largest copy comes first because a
    truncated export is often the smaller twin.
    """

    def rank(item: dict):
        path = str(item.get("path") or "").replace("\\", "/").lower()
        accidental = sum(
            2 for marker in ("/temp/", "/tmp/", "/appdata/", "/downloads/", "$recycle.bin")
            if marker in path
        )
        size = -int(item.get("size") or 0) if prefer_size else 0
        return (accidental, size, path.count("/"), -float(item.get("mtime") or 0.0))

    return min(items, key=rank)


@lru_cache(maxsize=1 << 18)
def _word_hash(word: str) -> int:
    """Stable 64-bit hash of one word, shared by every shingle that contains it."""
    return int.from_bytes(
        hashlib.blake2b(word.encode("utf-8"), digest_size=8).digest(), "big"
    )


def _simhash(text: str) -> int:
    """64-bit SimHash over three-word shingles; 0 when there is too little text.

    A shingle's value is mixed from the cached hashes of its three words instead
    of hashing the joined string: the blake2b cost is paid once per *distinct
    word*, not once per shingle, and the 64-bit vote runs over numpy columns
    rather than a Python bit loop. The result is a different (but equally stable
    and well-distributed) fingerprint than the old per-shingle digest; grouping
    near-identical documents is the same in practice, and a 4 000-document scan
    drops from ~13 s to ~1 s.
    """
    words = re.findall(r"\w+", text.lower())
    if len(words) < 12:
        return 0
    count = len(words) - 2
    hashes = np.fromiter(
        (_word_hash(word) for word in words), dtype=np.uint64, count=len(words)
    )
    values = hashes[:-2] * np.uint64(0x9E3779B97F4A7C15)
    values ^= hashes[1:-1] * np.uint64(0xC2B2AE3D27D4EB4F)
    values ^= hashes[2:] * np.uint64(0x165667B19E3779F9)
    values ^= values >> np.uint64(30)
    values *= np.uint64(0xBF58476D1CE4E5B9)
    values ^= values >> np.uint64(27)
    values *= np.uint64(0x94D049BB133111EB)
    values ^= values >> np.uint64(31)
    bits = np.unpackbits(values.view(np.uint8).reshape(-1, 8), axis=1)
    ones = bits.sum(axis=0, dtype=np.int64)
    out = 0
    for bit in range(64):
        if int(ones[bit]) * 2 > count:
            out |= 1 << bit
    return out


def normalize_path(path: str) -> str:
    """One spelling for a path: forward slashes, no trailing slash, casefolded.

    Windows is case-insensitive and accepts both separators, so `C:\\Docs\\a.PDF`
    and `c:/docs/a.pdf` are the same file and must compare equal. Repeated
    separators are collapsed too, because a path that travelled through a JSON
    round-trip or a shell is not always spelled the way the filesystem spells it.
    """
    return re.sub(r"/{2,}", "/", path.replace("\\", "/")).rstrip("/").casefold()


def _reraises_unless_missing(exc: sqlite3.OperationalError) -> None:
    """Only "no such table" means "this index predates the feature".

    Every personal-layer read is wrapped in a try because an old index has no
    `pins` table and must still open. But a locked database raises the same
    exception class, and swallowing it turned a busy index into "you have
    nothing pinned" — a wrong answer that looks like a real one. So the guard
    covers the missing table and nothing else.
    """
    if "no such table" not in str(exc).lower():
        raise exc


def _regexp(pattern: str, value: str | None) -> bool:
    if value is None:
        return False
    try:
        return re.search(pattern, value) is not None
    except re.error:
        return False


SCHEMA_VERSION = 12

# Contentless FTS: `content=''` keeps postings only — no copy of the corpus in
# the index. `contentless_delete=1` (SQLite 3.43+) makes `DELETE FROM` work,
# which is what lets a re-index replace one file's rows without rebuilding the
# whole table; older SQLite falls back to the `'delete'` command, which needs
# the original text and is issued by `_drop_chunks_of`.
#
# No `prefix='2 3'`: the prefix index cost 126 MB of the 346 MB file here, and
# measured *slower* for ordinary terms (more pages to read). Without it a
# 3-character prefix still range-scans the term dictionary in ~10 ms, and the
# in-RAM BM25 — which answers every query once it is built — expands prefixes
# from its own vocabulary anyway.
FTS_DELETE_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
    "text, tokenize=\"unicode61 remove_diacritics 2\", "
    "content='', contentless_delete=1)"
)
FTS_PLAIN_SQL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5("
    "text, tokenize=\"unicode61 remove_diacritics 2\", content='')"
)
# `fts5vocab` is a read-only view over the term dictionary: the typo channel
# reads it instead of keeping a 3-gram table (which cost 778 MB here).
VOCAB_SQL = "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab USING fts5vocab(chunks_fts, 'col')"



def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def prepare_vectors(conn: sqlite3.Connection, dim: int, writable: bool = True) -> str:
    """Attach sqlite-vec and make its tables match the schema. Returns the backend.

    Only used when the caller asked for the `sqlite-vec` backend; the default
    `pack` backend needs no extension at all.
    """
    attached = vecstore.load(conn)
    stored = _meta(conn, vecstore.BACKEND_META)

    if not attached:
        if stored == "sqlite-vec" and writable:
            raise RuntimeError(
                "this index stores vectors in sqlite-vec but the extension is not "
                "installed. Run: pip install sqlite-vec"
            )
        return "blob"

    if not writable:
        return "sqlite-vec" if stored == "sqlite-vec" else "blob"

    vecstore.create(conn, dim)
    if stored != "sqlite-vec" or vecstore.count(conn) == 0:
        vecstore.migrate_from_blobs(conn, dim)
    _set_meta(conn, vecstore.BACKEND_META, "sqlite-vec")
    return "sqlite-vec"


VIEW_SCHEMA = "-- The `files` view is the stable read/write surface every caller has always\n-- used; the INSTEAD OF triggers keep it a table in every way that matters.\nCREATE VIEW IF NOT EXISTS files AS\n    SELECT r.id AS id,\n           d.path || '\\' || r.name AS path,\n           r.name AS name,\n           r.char_mask AS char_mask,\n           r.size AS size,\n           r.mtime AS mtime,\n           r.sha256 AS sha256,\n           r.kind AS kind,\n           r.doc_date AS doc_date,\n           r.indexed_at AS indexed_at,\n           r.text_hash AS text_hash\n    FROM file_rows r JOIN dirs d ON d.dir_id = r.dir_id;\nCREATE TRIGGER IF NOT EXISTS files_insert INSTEAD OF INSERT ON files BEGIN\n    INSERT INTO dirs(path) VALUES (substr(NEW.path, 1, length(NEW.path) - length(NEW.name) - 1))\n        ON CONFLICT(path) DO NOTHING;\n    INSERT INTO file_rows(dir_id, name, char_mask, size, mtime, sha256, kind, doc_date, indexed_at, text_hash)\n    VALUES ((SELECT dir_id FROM dirs WHERE path = substr(NEW.path, 1, length(NEW.path) - length(NEW.name) - 1)),\n            NEW.name, NEW.char_mask, NEW.size, NEW.mtime, NEW.sha256, NEW.kind,\n            NEW.doc_date, NEW.indexed_at, NEW.text_hash)\n    ON CONFLICT(dir_id, name) DO UPDATE SET\n        char_mask = excluded.char_mask, size = excluded.size, mtime = excluded.mtime,\n        sha256 = excluded.sha256, kind = excluded.kind,\n        doc_date = COALESCE(excluded.doc_date, doc_date), indexed_at = excluded.indexed_at,\n        text_hash = excluded.text_hash;\nEND;\nCREATE TRIGGER IF NOT EXISTS files_update INSTEAD OF UPDATE ON files BEGIN\n    UPDATE file_rows SET\n        name = NEW.name, char_mask = NEW.char_mask, size = NEW.size, mtime = NEW.mtime,\n        sha256 = NEW.sha256, kind = NEW.kind, doc_date = NEW.doc_date,\n        indexed_at = NEW.indexed_at, text_hash = NEW.text_hash\n    WHERE id = OLD.id;\n    -- A relocated file: the row keeps its id, the directory reference moves.\n    INSERT INTO dirs(path) VALUES (substr(NEW.path, 1, length(NEW.path) - length(NEW.name) - 1))\n        ON CONFLICT(path) DO NOTHING;\n    UPDATE file_rows SET dir_id = (SELECT dir_id FROM dirs WHERE path = substr(NEW.path, 1, length(NEW.path) - length(NEW.name) - 1))\n    WHERE id = OLD.id AND NEW.path <> OLD.path;\n    DELETE FROM dirs WHERE dir_id NOT IN (SELECT DISTINCT dir_id FROM file_rows);\nEND;\nCREATE TRIGGER IF NOT EXISTS files_delete INSTEAD OF DELETE ON files BEGIN\n    DELETE FROM file_rows WHERE id = OLD.id;\n    DELETE FROM dirs WHERE dir_id NOT IN (SELECT DISTINCT dir_id FROM file_rows);\nEND;"


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
    version = 0
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error:
        pass
    _add_columns(conn, "images", IMAGE_COLUMNS)
    if _add_columns(conn, "file_texts", TEXT_BLOCK_COLUMNS):
        pass  # the rows are repacked right below
    # The legacy chain below assumes `files` is a real table; on a fresh v12
    # layout it is a view over dirs+file_rows and the chain must not touch it.
    layout = conn.execute("SELECT type FROM sqlite_master WHERE name = 'files'").fetchone()
    legacy_layout = bool(layout) and layout[0] == "table"
    if legacy_layout:
        if _add_columns(conn, "files", FILE_COLUMNS):
            rows = conn.execute("SELECT id, path FROM files").fetchall()
            conn.executemany(
                "UPDATE files SET name = ?, char_mask = ? WHERE id = ?",
                [(basename(row["path"]), char_mask(basename(row["path"])), row["id"]) for row in rows],
            )
        _add_columns(conn, "files", TEXT_COLUMNS)
        _repack_texts(conn)
        if version < 8:
            _quantize_vector_rows(conn)
        if version < 9:
            _migrate_v9(conn)
        if version < 10:
            _migrate_v10(conn)
        if version < 11:
            _migrate_v11(conn)
    if version < 12:
        _migrate_v12(conn)
    _ensure_fts(conn)
    if version < 9:
        # The dropped tiers (778 MB of trigram, 17 MB of CJK, the chunk text
        # column) only leave the file on a VACUUM. One pass, on the upgrade.
        try:
            conn.commit()
            conn.execute("VACUUM")
        except sqlite3.Error:
            pass
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _quantize_vector_rows(conn: sqlite3.Connection, batch: int = 4000) -> None:
    """Rewrite legacy float32 `vectors` blobs as i8+scale (schema v8).

    The table is the source of truth every backend derives from; at float32 it
    was the largest thing in the database. Rows are streamed, so the one-time
    pass on a 300k-chunk index costs a couple of minutes, not a memory spike.
    Already-quantized rows (length dim+4) are left alone, so the pass is a
    no-op after the first run.
    """
    try:
        pending: list[tuple[bytes, int]] = []
        rewritten = 0
        cursor = conn.execute(
            "SELECT rowid AS rid, dim, vec FROM vectors"
        )
        while True:
            rows = cursor.fetchmany(batch)
            if not rows:
                break
            for row in rows:
                dim = int(row["dim"] or 0)
                blob = row["vec"]
                if not dim or blob is None or len(blob) != dim * 4:
                    continue  # already i8, empty, or an unknown shape
                vector = np.frombuffer(blob, dtype=np.float32)
                pending.append((vecstore.quantize_vector(vector), int(row["rid"])))
            if pending:
                conn.executemany(
                    "UPDATE vectors SET vec = ? WHERE rowid = ?",
                    pending,
                )
                rewritten += len(pending)
                pending.clear()
        conn.commit()
        if rewritten:
            # The freed pages stay inside the file until vacuumed; on the real
            # 1.98 GB index this one-time pass returns about half a gigabyte.
            conn.execute("VACUUM")
    except sqlite3.Error:
        conn.rollback()  # a read-only file keeps its float32 rows; readers cope


# A document's text is compressed in fixed-size *character* blocks rather than
# one stream: a snippet then costs one 16 KB block instead of inflating the
# whole file. 16 384 characters is ~48 KB of UTF-8 here and still well inside
# zlib's 32 KB window, so the ratio is within a couple of percent of one stream.
TEXT_BLOCK_CHARS = 16384


def _pack_text(text: str) -> tuple[str, int, int, bytes, bytes]:
    """(content hash, length, block size, block offsets, compressed blocks).

    zlib, not zstd: it is in the standard library, so the frozen build and a
    bare `python -m core` behave the same. Level 6 lands within a few percent
    of zstd on prose and costs ~58 MB instead of the 322 MB of raw UTF-8.
    Packing runs in the extraction workers, which are never the indexing
    bottleneck, so the extra level costs no wall time; level 3 was measured
    5 MB larger on the benchmark corpus for no speedup.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    offsets = [0]
    blocks: list[bytes] = []
    total = 0
    for start in range(0, len(text), TEXT_BLOCK_CHARS):
        block = zlib.compress(text[start:start + TEXT_BLOCK_CHARS].encode("utf-8"), 6)
        blocks.append(block)
        total += len(block)
        offsets.append(total)
    return (
        digest,
        len(text),
        TEXT_BLOCK_CHARS,
        np.asarray(offsets, dtype=np.int32).tobytes(),
        b"".join(blocks),
    )


def _data_of(row) -> bytes:
    """`file_texts.data`, unsealed when paranoid mode sealed it.

    Sealed rows carry the `AFS1` prefix, so the check costs nothing and both
    layouts answer through the same call sites."""
    data = row["data"]
    if vault_mod.Vault.is_sealed(data):
        vault = vault_mod.current()
        if vault is None:
            raise vault_mod.VaultError(
                "the index is sealed but the vault key is not loaded; "
                "run 'autofind seal status' or restore vault.key"
            )
        return vault.open_bytes(data)
    return data


def _unpack_text(blob: bytes | None) -> str:
    """A whole document from a legacy single-stream blob."""
    if not blob:
        return ""
    try:
        return zlib.decompress(blob).decode("utf-8", "replace")
    except zlib.error:
        return ""


def _row_slice(row: sqlite3.Row, start: int, end: int) -> str:
    """`text[start:end]` from a `file_texts` row, inflating only its blocks.

    Rows written before block compression (`block_size = 0`) fall back to
    inflating the whole stream; `_repack_texts` upgrades them on first open.
    """
    data = _data_of(row)
    block_size = int(row["block_size"] or 0)
    offsets = row["offsets"]
    if not data:
        return ""
    if start < 0:
        start = 0
    if end <= start:
        return ""
    if not block_size or not offsets:
        return _unpack_text(data)[start:end]
    # Plain stdlib array, not numpy: `_row_slice` runs for every snippet, and a
    # one-shot CLI search should not import numpy (70 ms) just to read four
    # block offsets.
    table = array("i")
    table.frombytes(offsets)
    if len(table) < 2:
        return _unpack_text(data)[start:end]
    first = start // block_size
    last = min((end - 1) // block_size, len(table) - 2)
    parts: list[str] = []
    for index in range(first, last + 1):
        lo, hi = int(table[index]), int(table[index + 1])
        try:
            block = zlib.decompress(data[lo:hi]).decode("utf-8", "replace")
        except zlib.error:
            return ""
        base = index * block_size
        a = max(start - base, 0)
        b = min(end - base, len(block))
        if b > a:
            parts.append(block[a:b])
    return "".join(parts)


def _row_text(row: sqlite3.Row) -> str:
    data = _data_of(row)
    if not data:
        return ""
    if not int(row["block_size"] or 0):
        return _unpack_text(data)
    return _row_slice(row, 0, int(row["raw_len"] or 0))


def _text_row(conn: sqlite3.Connection, digest: str | None) -> sqlite3.Row | None:
    if not digest:
        return None
    return conn.execute(
        "SELECT hash, raw_len, block_size, offsets, data FROM file_texts WHERE hash = ?",
        (digest,),
    ).fetchone()


def _read_text(conn: sqlite3.Connection, digest: str | None) -> str:
    row = _text_row(conn, digest)
    return _row_text(row) if row else ""


def _iter_chunk_texts(conn: sqlite3.Connection) -> Iterator[tuple[int, str]]:
    """(chunk id, its text) for every chunk, decompressing one file at a time.

    Rows arrive grouped by file, so a document's blob is inflated once and its
    chunks are slices of that one string — the whole reason the text lives per
    file rather than per chunk.
    """
    cursor = conn.execute(
        "SELECT c.id, c.start, c.len, f.text_hash FROM chunks c "
        "JOIN files f ON f.id = c.file_id ORDER BY c.file_id, c.ord"
    )
    current: str | None = None
    text = ""
    for row in cursor:
        digest = row["text_hash"]
        if digest != current:
            current = digest
            text = _read_text(conn, digest)
        start = int(row["start"])
        yield int(row["id"]), text[start:start + int(row["len"])]


def _fts_sql(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = 'chunks_fts'"
    ).fetchone()
    return (row[0] or "") if row else None


def _ensure_vocab(conn: sqlite3.Connection) -> None:
    try:
        conn.execute(VOCAB_SQL)
    except sqlite3.Error:
        pass  # no fts5vocab in this build: the typo channel is simply absent


def _ensure_fts(conn: sqlite3.Connection) -> None:
    """Make the single contentless FTS tier exist and be complete.

    A v9 database has one table with postings only: the text it indexes is a
    span into `file_texts`, so an external-content table would have had to read
    a column that no longer exists. Anything older (or half-built) is dropped
    and refilled — that is one pass over the corpus, not a re-extraction.
    """
    if _meta(conn, "sealed") == "1":
        # Paranoid mode: no postings on disk. Content search rides the
        # in-RAM BM25; recreating FTS here would write the whole vocabulary
        # back in plaintext.
        return
    sql = _fts_sql(conn)
    if sql is not None:
        flat = sql.replace(" ", "").lower()
        contentless = "content=''" in flat or 'content=""' in flat
        # A table with a prefix index is a v9 beta: correct but 126 MB bigger
        # than it needs to be, so it is rebuilt once, like any half-built one.
        if contentless and "prefix=" not in flat:
            supports_delete = "contentless_delete=1" in flat
            _set_meta(conn, "fts_delete", "1" if supports_delete else "0")
            _ensure_vocab(conn)
            if _meta(conn, "fts_v9") == "1":
                return
    _rebuild_fts(conn)


def _rebuild_fts(conn: sqlite3.Connection, batch: int = 4000) -> None:
    """Drop and refill `chunks_fts` from the chunk spans."""
    conn.execute("DROP TABLE IF EXISTS chunks_vocab")
    conn.execute("DROP TABLE IF EXISTS chunks_fts")
    try:
        conn.execute(FTS_DELETE_SQL)
        _set_meta(conn, "fts_delete", "1")
    except sqlite3.Error:
        conn.execute(FTS_PLAIN_SQL)
        _set_meta(conn, "fts_delete", "0")
    pending: list[tuple[int, str]] = []
    for chunk_id, text in _iter_chunk_texts(conn):
        # CJK runs become bigrams here, in the same index as the latin words:
        # one tier, not the main table plus a full copy of the CJK subset.
        pending.append((chunk_id, cjk_bigrams(text)))
        if len(pending) >= batch:
            conn.executemany("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", pending)
            pending.clear()
    if pending:
        conn.executemany("INSERT INTO chunks_fts(rowid, text) VALUES(?,?)", pending)
    _ensure_vocab(conn)
    _set_meta(conn, "fts_v9", "1")


def _fold_chunk_text(conn: sqlite3.Connection, batch: int = 2000) -> None:
    """v9 step one: move chunk text into one blob per file, keep the spans.

    Chunks overlap, so the text is rebuilt as their newline join and every
    chunk keeps the span it occupies in it. The result is not byte-identical to
    the extracted document (the overlap stays), but it is exact for snippets,
    regex and the FTS rebuild — and it needs no re-extraction of 14 000 files.
    """
    conn.execute("DROP TABLE IF EXISTS chunks_v9")
    conn.execute(
        "CREATE TABLE chunks_v9 (id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, "
        "ord INTEGER NOT NULL, start INTEGER NOT NULL, len INTEGER NOT NULL)"
    )
    cursor = conn.execute(
        "SELECT id, file_id, ord, text FROM chunks ORDER BY file_id, ord"
    )
    chunk_rows: list[tuple[int, int, int, int, int]] = []
    text_rows: dict[str, tuple[str, int, int, bytes, bytes]] = {}
    hashes: dict[int, str] = {}
    file_id: int | None = None
    parts: list[str] = []
    ids: list[int] = []
    ords: list[int] = []

    def flush_file() -> None:
        if file_id is None or not parts:
            return
        text = "\n".join(parts)
        packed = _pack_text(text)
        text_rows[packed[0]] = packed
        hashes[file_id] = packed[0]
        position = 0
        for chunk_id, order, part in zip(ids, ords, parts):
            chunk_rows.append((chunk_id, file_id, order, position, len(part)))
            position += len(part) + 1
        parts.clear()
        ids.clear()
        ords.clear()

    def flush_batch() -> None:
        if chunk_rows:
            conn.executemany(
                "INSERT INTO chunks_v9(id, file_id, ord, start, len) VALUES(?,?,?,?,?)",
                chunk_rows,
            )
            chunk_rows.clear()
        if text_rows:
            conn.executemany(
                "INSERT INTO file_texts(hash, raw_len, block_size, offsets, data) "
                "VALUES(?,?,?,?,?) ON CONFLICT(hash) DO NOTHING",
                list(text_rows.values()),
            )
            text_rows.clear()

    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            break
        for row in rows:
            if row["file_id"] != file_id:
                flush_file()
                file_id = int(row["file_id"])
            parts.append(row["text"] or "")
            ids.append(int(row["id"]))
            ords.append(int(row["ord"]))
        flush_batch()
    flush_file()
    flush_batch()
    cursor.close()
    conn.execute("DROP TABLE chunks")
    conn.execute("ALTER TABLE chunks_v9 RENAME TO chunks")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id)")
    conn.executemany(
        "UPDATE files SET text_hash = ? WHERE id = ?",
        [(digest, fid) for fid, digest in hashes.items()],
    )


def _migrate_file_vectors(conn: sqlite3.Connection, batch: int = 20000) -> None:
    """v9 step two: mean-pool the per-chunk vectors into one binary row per file."""
    try:
        pending = conn.execute(
            "SELECT COUNT(*) FROM vectors WHERE owner_type='chunk'"
        ).fetchone()[0]
    except sqlite3.Error:
        return
    if not pending:
        return
    file_of = {int(row[0]): int(row[1]) for row in conn.execute("SELECT id, file_id FROM chunks")}
    sums: dict[int, np.ndarray] = {}
    counts: dict[int, int] = {}
    cursor = conn.execute("SELECT owner_id, dim, vec FROM vectors WHERE owner_type='chunk'")
    while True:
        rows = cursor.fetchmany(batch)
        if not rows:
            break
        for row in rows:
            file_id = file_of.get(int(row["owner_id"]))
            dim = int(row["dim"] or 0)
            if file_id is None or not dim:
                continue
            vector = vecstore.dequantize_vector(row["vec"], dim)
            if file_id in sums:
                sums[file_id] += vector
                counts[file_id] += 1
            else:
                sums[file_id] = vector.copy()
                counts[file_id] = 1
    packed = [
        (file_id, int(vector.shape[0]), filevec.to_bits(vector / counts[file_id]))
        for file_id, vector in sums.items()
    ]
    conn.executemany(
        "INSERT INTO file_vectors(file_id, dim, bits) VALUES(?,?,?) "
        "ON CONFLICT(file_id) DO UPDATE SET dim=excluded.dim, bits=excluded.bits",
        packed,
    )
    conn.execute("DELETE FROM vectors WHERE owner_type='chunk'")


def _repack_texts(conn: sqlite3.Connection) -> None:
    """Upgrade whole-stream `file_texts` rows to block compression.

    Only an index built by the first cut of v9 has such rows; a fresh one is
    written in blocks from the start. The text is unchanged, so the hash (and
    every `files.text_hash`) stays valid.
    """
    try:
        rows = conn.execute(
            "SELECT hash, data FROM file_texts "
            "WHERE block_size = 0 OR offsets IS NULL OR length(offsets) < 8"
        ).fetchall()
    except sqlite3.Error:
        return
    if not rows:
        return
    updates = []
    for row in rows:
        data = row["data"]
        if vault_mod.Vault.is_sealed(data):
            continue  # sealed rows are already block-compressed; repacking plaintext would corrupt them
        text = _unpack_text(data)
        if not text:
            continue
        _, raw_len, block_size, offsets, packed = _pack_text(text)
        updates.append((raw_len, block_size, offsets, packed, row["hash"]))
    conn.executemany(
        "UPDATE file_texts SET raw_len=?, block_size=?, offsets=?, data=? WHERE hash=?",
        updates,
    )
    conn.commit()


def _migrate_v10(conn: sqlite3.Connection) -> None:
    """Schema v10: finish content-addressing the shared texts.

    v9 already stores a document's text once per content hash; what was
    missing was the bookkeeping around it. Two one-time steps:

    * the refcount index on `files.text_hash` (the column arrives with the
      v9 migration, so the index belongs here, not in SCHEMA), so dropping
      a file can ask "who else still holds this text?" in one seek instead
      of a scan;
    * a sweep of orphaned `file_texts` rows — texts no file references
      anymore, left behind by deletes from before the refcount check
      existed. They were invisible in every count but still on disk.
    """
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_text_hash ON files(text_hash)")
    try:
        removed = conn.execute(
            "DELETE FROM file_texts WHERE hash NOT IN "
            "(SELECT text_hash FROM files WHERE text_hash IS NOT NULL)"
        ).rowcount
    except sqlite3.Error:
        removed = 0
    if removed and removed > 0:
        # The freed pages only leave the file on a VACUUM; this runs once.
        try:
            conn.commit()
            conn.execute("VACUUM")
        except sqlite3.Error:
            pass


def _migrate_v9(conn: sqlite3.Connection) -> None:
    """Schema v9: text once per file, chunks as spans, one FTS, binary vectors.

    Everything here is derived from what the old database already holds, so an
    existing index upgrades in one pass — no file is read from disk again.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(chunks)")}
    if "text" in columns:
        _fold_chunk_text(conn)
    for table in ("chunks_cjk", "chunks_tri", "chunks_vocab", "chunks_fts"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute("DELETE FROM meta WHERE key IN ('trigram', 'aux_fts_backfill', 'fts_v9')")
    _migrate_file_vectors(conn)
    conn.commit()


def _migrate_v12(conn: sqlite3.Connection) -> None:
    """Schema v12: the directory prefixes move to their own table, the file
    row keeps only the basename, and every content hash becomes a 32-byte
    BLOB instead of a 64-character hex string.

    The measured shape on the 100k corpus: 100k files shared 100 directory
    prefixes, and the path column plus its UNIQUE autoindex cost ~25 MB of
    duplicated text. `files` becomes a view with INSTEAD OF triggers, so the
    read/write surface every caller uses stays exactly the same.
    """
    conn.execute("PRAGMA foreign_keys=OFF")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='dirs'").fetchone():
        conn.execute(
            "CREATE TABLE dirs (dir_id INTEGER PRIMARY KEY, path TEXT NOT NULL UNIQUE)"
        )
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='file_rows'").fetchone():
        conn.execute(
            "CREATE TABLE file_rows ("
            "id INTEGER PRIMARY KEY, dir_id INTEGER NOT NULL REFERENCES dirs(dir_id), "
            "name TEXT NOT NULL, char_mask INTEGER, size INTEGER NOT NULL, mtime REAL NOT NULL, "
            "sha256 BLOB NOT NULL, kind TEXT NOT NULL, doc_date TEXT, indexed_at REAL NOT NULL, "
            "text_hash BLOB, UNIQUE(dir_id, name))"
        )
    legacy = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'files'"
    ).fetchone()
    if legacy is not None and legacy[0] == "table":
        # A path splits into directory + basename exactly: the row already
        # carries `name`, so the prefix is pure arithmetic - no separator
        # escaping, no instr-first-vs-last traps.
        conn.execute(
            "INSERT OR IGNORE INTO dirs(path) "
            "SELECT DISTINCT substr(path, 1, length(path) - length(name) - 1) "
            "FROM files WHERE name IS NOT NULL"
        )
        conn.execute(
            "INSERT OR IGNORE INTO file_rows(id, dir_id, name, char_mask, size, mtime, "
            "sha256, kind, doc_date, indexed_at, text_hash) "
            "SELECT f.id, d.dir_id, f.name, f.char_mask, f.size, f.mtime, "
            "unhex(f.sha256), f.kind, f.doc_date, f.indexed_at, unhex(f.text_hash) "
            "FROM files f JOIN dirs d ON d.path = substr(f.path, 1, length(f.path) - length(f.name) - 1)"
        )
        conn.execute("DROP TABLE files")
        # The text store: same rows, the key becomes the 32 raw bytes.
        conn.execute("ALTER TABLE file_texts RENAME TO file_texts_v11")
        conn.execute(
            "CREATE TABLE file_texts ("
            "hash BLOB PRIMARY KEY, raw_len INTEGER NOT NULL, "
            "block_size INTEGER NOT NULL DEFAULT 0, offsets BLOB NOT NULL DEFAULT x'', "
            "data BLOB NOT NULL)"
        )
        conn.execute(
            "INSERT INTO file_texts SELECT unhex(hash), raw_len, block_size, offsets, data "
            "FROM file_texts_v11"
        )
        conn.execute("DROP TABLE file_texts_v11")
        conn.execute(
            "UPDATE images SET sha256 = unhex(sha256) "
            "WHERE sha256 IS NOT NULL AND typeof(sha256) = 'text'"
        )
        # The child tables carry `REFERENCES files(id)`, and a view cannot
        # back a foreign key: each one is recreated against file_rows, rows
        # copied verbatim.
        for table, columns, target in (
            ("chunks", "id, file_id, ord, start, len", "chunks_v12"),
            ("file_vectors", "file_id, dim, bits", "file_vectors_v12"),
            ("images", "id, path, file_id, sha256, width, height, size, mtime, thumb, "
             "ocr_text, ocr_boxes, ocr_engine, phash, indexed_at", "images_v12"),
            ("usage", "file_id, opens, last_open", "usage_v12"),
            ("pins", "file_id, added_at", "pins_v12"),
            ("tags", "file_id, tag, added_at", "tags_v12"),
            ("collection_files", "collection_id, file_id, added_at", "collection_files_v12"),
            ("symbols", "id, file_id, name, lowered, kind, line, text", "symbols_v12"),
        ):
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
            ).fetchone()
            if ddl is None:
                continue
            create = ddl[0].replace("REFERENCES files(id)", "REFERENCES file_rows(id)")
            conn.execute(f"ALTER TABLE {table} RENAME TO {target}")
            conn.execute(create)
            conn.execute(f"INSERT OR IGNORE INTO {table} SELECT {columns} FROM {target}")
            conn.execute(f"DROP TABLE {target}")
    conn.execute("DROP INDEX IF EXISTS idx_files_text_hash")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_text_hash ON file_rows(text_hash)")
    # The v10 orphan sweep is part of this upgrade too: a database can sit at
    # v11 with sweep work still owed, and the hash conversion must not carry
    # unreferenced texts across.
    conn.execute(
        "DELETE FROM file_texts WHERE hash NOT IN "
        "(SELECT text_hash FROM file_rows WHERE text_hash IS NOT NULL)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_files_mtime")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_mtime ON file_rows(mtime)")
    conn.execute("DROP INDEX IF EXISTS idx_files_size")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON file_rows(size)")
    conn.commit()
    conn.executescript(VIEW_SCHEMA)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")


def _migrate_v11(conn: sqlite3.Connection) -> None:
    """Schema v11: the personal-layer tables (notes, tag_colors) are additive
    and arrive with executescript(SCHEMA); the migration re-runs the v10
    orphan sweep because it is idempotent and a database can sit at v10 with
    sweep work still owed (an interrupted upgrade)."""
    _migrate_v10(conn)


class Store:
    def __init__(
        self,
        path: str | Path,
        embedder: str = "hashing-384",
        dim: int = 384,
        readonly: bool = False,
        cache: "VectorCache | None" = None,
        vector_backend: str = "pack",
        ram_bm25: bool | None = None,
    ) -> None:
        self.path = Path(path)
        self.readonly = readonly
        self.cache = cache
        self._ram_bm25 = _RAM_BM25_ENABLED if ram_bm25 is None else bool(ram_bm25)
        self._requested_backend = vector_backend
        if readonly:
            uri = f"file:{self.path.as_posix()}?mode=ro"
            self.conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        else:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.create_function("regexp", 2, _regexp, deterministic=True)
        self._vec_key: tuple | None = None
        self._vec_gen = 0  # bumped by `_invalidate_vectors`, part of that key
        self._file_key: tuple[int, int] | None = None
        self._checkout = None  # the pack this Store currently holds from PACKS
        self.dim = dim
        self._fts_delete = True  # contentless FTS supports DELETE FROM
        vecstore.PACKS.ref(self.path)
        self._vec_backend = "filebin"
        # Read-heavy tuning, measured against the forums' standard set: mmap
        # lets SQLite read pages without a read() syscall (the FTS index and
        # vector blobs are exactly such cold reads), and a real page cache
        # keeps the hot vocabulary resident. busy_timeout softens the watcher
        # racing a manual index run for the write lock.
        self.conn.execute("PRAGMA mmap_size=0" if readonly else "PRAGMA mmap_size=1073741824")
        # A read-only connection keeps its pages in the OS page cache anyway
        # (the mmap above), so a private 32 MB cache is 24 MB of duplicated
        # RSS per open Store. Writers keep the bigger cache: indexing is the
        # workload where the hot vocabulary actually pays.
        self.conn.execute("PRAGMA cache_size=-4000" if readonly else "PRAGMA cache_size=-32000")
        self.conn.execute("PRAGMA busy_timeout=5000")
        if not readonly:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self._migrate()
            self._check_embedder(embedder, dim)
            self._vec_backend = self._setup_vectors(dim, writable=True)
        else:
            if self._schema_version() < SCHEMA_VERSION:
                # A reader that opens an index built by an older version still has
                # to migrate it, otherwise every `usage` or `char_mask` query fails.
                # It costs one short writable connection, once, after an upgrade.
                self._migrate_externally()
            self._vec_backend = self._setup_vectors(dim, writable=False)
        self._fts_delete = self._meta_flag("fts_delete", True)
        self.conn.commit()

    def _meta_flag(self, key: str, default: bool) -> bool:
        value = _meta(self.conn, key)
        if value is None:
            return default
        return value == "1"

    def _setup_vectors(self, dim: int, writable: bool) -> str:
        """Documents always use `filebin` since v9.

        One binary vector per file needs no index and no side file: 14 000 rows
        is a 5 MB matvec, which beats any structure that has to be built,
        versioned and remapped. The `vector_backend` setting is kept for the
        image vectors, which still go through `vectors`/`sqlite-vec`.
        """
        return "filebin"

    # ---------------------------------------------------------------- schema

    @property
    def has_trigram(self) -> bool:
        """Kept for callers: the 3-gram tier is gone (it cost 778 MB here).

        The typo channel now reads the FTS vocabulary instead of a second copy
        of the corpus, so there is no table to check.
        """
        return False

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
            # Vector migration is deliberately NOT here: it depends on the
            # backend the caller asked for, and running it eagerly copied 217k
            # vectors into vec0 on every upgrade (7.6 s, +333 MB) for users who
            # never selected that backend.
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

    def self_test(self) -> dict:
        """Integrity check: counts, orphans, sealed state."""
        report = {"ok": True, "issues": []}
        try:
            for name, sql in (
                ("files", "SELECT COUNT(*) FROM files"),
                ("chunks", "SELECT COUNT(*) FROM chunks"),
                ("texts", "SELECT COUNT(*) FROM file_texts"),
                ("images", "SELECT COUNT(*) FROM images"),
            ):
                report[name] = int(self.conn.execute(sql).fetchone()[0])
            orphans = self.conn.execute(
                "SELECT COUNT(*) FROM chunks c LEFT JOIN files f ON f.id = c.file_id "
                "WHERE f.id IS NULL"
            ).fetchone()[0]
            if orphans:
                report["issues"].append(f"{orphans} orphan chunks")
            orphan_imgs = self.conn.execute(
                "SELECT COUNT(*) FROM images i LEFT JOIN files f ON f.id = i.file_id "
                "WHERE f.id IS NULL"
            ).fetchone()[0]
            if orphan_imgs:
                report["issues"].append(f"{orphan_imgs} orphan images")
            empty = self.conn.execute(
                "SELECT COUNT(*) FROM file_texts WHERE data IS NULL OR data = ''"
            ).fetchone()[0]
            if empty:
                report["issues"].append(f"{empty} empty text rows")
            report["ok"] = len(report["issues"]) == 0
        except Exception as exc:
            report["ok"] = False
            report["issues"].append(str(exc))
        return report

    @property
    def sealed(self) -> bool:
        """Paranoid mode: content blobs are sealed at rest."""
        try:
            return self._meta_flag("sealed", False)
        except Exception:
            return False

    def save_search_query(self, q: str) -> None:
        """Store a search query in the recent searches table (max 10).

        A repeat of the newest entry writes nothing: the client fires a search
        per settled keystroke, and every one of those commits moves the WAL,
        which the BM25 stamp check treats as corpus churn."""
        import time as _t
        clean = q.strip()
        if not clean:
            return
        last = self.conn.execute(
            "SELECT q FROM recent_searches ORDER BY at DESC LIMIT 1"
        ).fetchone()
        if last is not None and str(last[0]) == clean:
            return
        self.conn.execute(
            "INSERT INTO recent_searches(q, at) VALUES(?, ?)",
            (clean, _t.time())
        )
        self.conn.execute(
            "DELETE FROM recent_searches WHERE at NOT IN "
            "(SELECT at FROM recent_searches ORDER BY at DESC LIMIT 10)"
        )
        self.conn.commit()

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
        # The indexed form: dirs lookup by unique path, then the thin
        # (dir_id, name) index - a view filter on the computed path column
        # would concatenate all 100k rows first.
        dirpath, name = _split_path(path)
        return self.conn.execute(
            "SELECT r.id AS id, d.path || '\\' || r.name AS path, r.name AS name, "
            "r.char_mask AS char_mask, r.size AS size, r.mtime AS mtime, "
            "r.sha256 AS sha256, r.kind AS kind, r.doc_date AS doc_date, "
            "r.indexed_at AS indexed_at, r.text_hash AS text_hash "
            "FROM file_rows r JOIN dirs d ON d.dir_id = r.dir_id "
            "WHERE d.path = ? AND r.name = ?",
            (dirpath, name),
        ).fetchone()

    def file_by_id(self, file_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()

    def upsert_file(
        self, path: str, size: int, mtime: float, sha256: str, kind: str,
        doc_date: str | None = None,
    ) -> int:
        # Writes go straight to dirs+file_rows: SQLite refuses an UPSERT
        # against the `files` view, and this is the one writer that needs it.
        dirpath, name = _split_path(path)
        self.conn.execute(
            "INSERT INTO dirs(path) VALUES(?) ON CONFLICT(path) DO NOTHING",
            (dirpath,),
        )
        dir_id = int(self.conn.execute(
            "SELECT dir_id FROM dirs WHERE path = ?", (dirpath,)
        ).fetchone()[0])
        self.conn.execute(
            "INSERT INTO file_rows(dir_id, name, char_mask, size, mtime, sha256, kind, doc_date, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(dir_id, name) DO UPDATE SET char_mask=excluded.char_mask, "
            "size=excluded.size, mtime=excluded.mtime, sha256=excluded.sha256, "
            "kind=excluded.kind, doc_date=COALESCE(excluded.doc_date, doc_date), "
            "indexed_at=excluded.indexed_at",
            (dir_id, name, char_mask(name), size, mtime, sha256, kind, doc_date, time.time()),
        )
        row = self.conn.execute(
            "SELECT id FROM file_rows WHERE dir_id = ? AND name = ?", (dir_id, name)
        ).fetchone()
        return int(row["id"])

    def doc_dates(self, file_ids: Iterable[int]) -> dict[int, str]:
        """The document-claimed date per file id, when the index knows one."""
        ids = [int(fid) for fid in file_ids]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT id, doc_date FROM files WHERE id IN ({marks}) AND doc_date IS NOT NULL",
            ids,
        ).fetchall()
        return {int(row["id"]): str(row["doc_date"]) for row in rows}

    def files_without_doc_date(self, limit: int = 3000) -> list[int]:
        return [
            int(row["id"])
            for row in self.conn.execute(
                "SELECT id FROM files WHERE doc_date IS NULL LIMIT ?", (limit,)
            )
        ]

    def set_doc_date(self, file_id: int, doc_date: str) -> None:
        self.conn.execute(
            "UPDATE files SET doc_date = ? WHERE id = ?", (doc_date, int(file_id))
        )

    def chunk_heads(self, file_id: int, limit: int = 3) -> str:
        """The first chunks' text: a document's date lives in its header."""
        rows = self.conn.execute(
            "SELECT c.start, c.len, f.text_hash FROM chunks c "
            "JOIN files f ON f.id = c.file_id WHERE c.file_id = ? ORDER BY c.ord LIMIT ?",
            (int(file_id), limit),
        ).fetchall()
        if not rows:
            return ""
        text = self.text_of(rows[0]["text_hash"])
        return "\n".join(
            text[int(row["start"]):int(row["start"]) + int(row["len"])] for row in rows
        )

    # ------------------------------------------------------------------ text

    def text_of(self, text_hash: str | None) -> str:
        """The stored document text behind a `files.text_hash`, or ""."""
        return _read_text(self.conn, text_hash)

    def text_rows(self, hashes: Iterable[str | None]) -> dict[str, sqlite3.Row]:
        """The `file_texts` rows for several hashes, in as few queries as fit."""
        wanted = [h for h in hashes if h]
        if not wanted:
            return {}
        out: dict[str, sqlite3.Row] = {}
        for start in range(0, len(wanted), 200):
            block = wanted[start:start + 200]
            rows = self.conn.execute(
                "SELECT hash, raw_len, block_size, offsets, data FROM file_texts "
                f"WHERE hash IN ({','.join('?' * len(block))})",
                block,
            ).fetchall()
            for row in rows:
                out[row["hash"]] = row
        return out

    def texts_of(self, hashes: Iterable[str | None]) -> dict[str, str]:
        """Inflate several stored texts in one query, keyed by hash."""
        return {digest: _row_text(row) for digest, row in self.text_rows(hashes).items()}

    def file_text(self, file_id: int) -> str:
        row = self.conn.execute(
            "SELECT text_hash FROM files WHERE id = ?", (int(file_id),)
        ).fetchone()
        return self.text_of(row["text_hash"]) if row else ""

    def replace_chunks(
        self,
        file_id: int,
        text: str | Sequence[str],
        spans: Sequence[tuple[int, int]] | np.ndarray | None = None,
        vectors: np.ndarray | None = None,
        packed: tuple[str, int, int, bytes, bytes] | None = None,
    ) -> int:
        """Write one document: its text once, its chunk spans, its vector.

        `text` is the extracted document; `spans` are half-open offsets into
        its normalised form (`core.chunker.chunk_spans`). A plain list of chunk
        strings is still accepted — the spans are then their newline join —
        which keeps older callers and tests honest without a second code path.

        `vectors` may be one document vector, a per-chunk matrix (mean-pooled
        here) or None. Only the file-level result is stored.
        """
        if isinstance(spans, np.ndarray):
            # Older callers pass the vector matrix as the third positional
            # argument (`replace_chunks(fid, chunks, matrix)`); it is a vector,
            # never a span list.
            if vectors is None:
                vectors = spans
            spans = None
        if not isinstance(text, str):
            parts = [str(part).strip() for part in text]
            # Normalise the join first so the spans below index the same string
            # that is stored (normalize strips the ends).
            document = normalize("\n".join(parts))
            spans, position = [], 0
            for part in parts:
                spans.append((position, position + len(part)))
                position += len(part) + 1
        else:
            document = normalize(text)
        if spans is None:
            spans = chunk_spans(document)
        self._drop_chunks_of(file_id)
        if not document or not spans:
            self.conn.execute("UPDATE files SET text_hash = NULL WHERE id = ?", (int(file_id),))
            self._invalidate_vectors()
            return 0

        digest, raw_len, block_size, offsets, data = (
            packed if packed is not None else _pack_text(document)
        )
        # Content-addressed: a second copy of the same document costs one row
        # in `files`, not another copy of its text or its postings. Under
        # seal the blob goes down as AES-256-GCM ciphertext (the digest stays
        # a plaintext-content hash - a list of content hashes leaks nothing).
        if self.sealed:
            vault = vault_mod.current()
            if vault is None:
                raise vault_mod.VaultError(
                    "the index is sealed but the vault key is not loaded"
                )
            data = vault.seal(data)
        self.conn.execute(
            "INSERT INTO file_texts(hash, raw_len, block_size, offsets, data) "
            "VALUES(?,?,?,?,?) ON CONFLICT(hash) DO NOTHING",
            (digest, raw_len, block_size, offsets, data),
        )
        self.conn.execute(
            "UPDATE files SET text_hash = ? WHERE id = ?", (digest, int(file_id))
        )
        self.conn.executemany(
            "INSERT INTO chunks(file_id, ord, start, len) VALUES(?,?,?,?)",
            [
                (int(file_id), order, int(start), int(end) - int(start))
                for order, (start, end) in enumerate(spans)
            ],
        )
        chunk_ids = [int(row["id"]) for row in self.conn.execute(
            "SELECT id FROM chunks WHERE file_id = ? ORDER BY ord", (int(file_id),)
        )]
        if not self.sealed:
            self.conn.executemany(
                "INSERT INTO chunks_fts(rowid, text) VALUES(?,?)",
                [
                    (chunk_id, cjk_bigrams(document[int(start):int(end)]))
                    for chunk_id, (start, end) in zip(chunk_ids, spans)
                ],
            )
        if vectors is not None:
            vector = filevec.mean_pool(np.asarray(vectors, dtype=np.float32))
            if vector.size:
                self.conn.execute(
                    "INSERT INTO file_vectors(file_id, dim, bits) VALUES(?,?,?) "
                    "ON CONFLICT(file_id) DO UPDATE SET dim=excluded.dim, bits=excluded.bits",
                    (int(file_id), int(vector.shape[0]), filevec.to_bits(vector)),
                )
        self._invalidate_vectors()
        return len(chunk_ids)

    def _drop_chunks_of(self, file_id: int) -> None:
        file_id = int(file_id)
        row = self.conn.execute(
            "SELECT text_hash FROM files WHERE id = ?", (file_id,)
        ).fetchone()
        old_hash = row["text_hash"] if row else None
        old_ids = [int(row["id"]) for row in self.conn.execute(
            "SELECT id FROM chunks WHERE file_id = ?", (file_id,)
        )]
        if old_ids:
            # Sealed mode dropped the FTS tables entirely (their postings are
            # plaintext); touching them here would raise "no such table" on
            # every update of an already-indexed file.
            if self._fts_delete and not self.sealed:
                self.conn.executemany(
                    "DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in old_ids]
                )
            elif not self.sealed:
                # Pre-3.43 SQLite: a contentless table can only unindex a row
                # through the `'delete'` command, which wants the text back.
                rows = self.conn.execute(
                    "SELECT id, start, len FROM chunks WHERE file_id = ? ORDER BY ord",
                    (file_id,),
                ).fetchall()
                text = self.text_of(old_hash)
                self.conn.executemany(
                    "INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES('delete', ?, ?)",
                    [
                        (
                            int(chunk["id"]),
                            cjk_bigrams(
                                text[int(chunk["start"]):int(chunk["start"]) + int(chunk["len"])]
                            ),
                        )
                        for chunk in rows
                    ],
                )
            self.conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        self.conn.execute("DELETE FROM file_vectors WHERE file_id = ?", (file_id,))
        if old_hash:
            still = self.conn.execute(
                "SELECT 1 FROM files WHERE text_hash = ? AND id <> ? LIMIT 1",
                (old_hash, file_id),
            ).fetchone()
            if still is None:
                self.conn.execute("DELETE FROM file_texts WHERE hash = ?", (old_hash,))

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

    def duplicate_images(self, limit: int = 30, per_group: int = 500) -> list[dict]:
        """Exact copies: same file name, same byte size and same digest.

        The perceptual hash is deliberately not used here — it groups screens
        that merely look alike, which is not what "duplicate" means in this
        report. Exact copies are byte-identical, so the digest settles it, and
        the name and size keep unrelated images with a digest collision out.
        """
        rows = self.conn.execute(
            "SELECT id AS file_id, path, size, mtime, sha256 FROM files "
            "WHERE kind = 'image' ORDER BY id"
        ).fetchall()
        groups: dict[tuple[str, int, str], list[dict]] = {}
        for row in rows:
            path = str(row["path"])
            name = basename(path).casefold()
            key = (name, int(row["size"]), str(row["sha256"]))
            groups.setdefault(key, []).append({
                "file_id": int(row["file_id"]),
                "path": path,
                "size": int(row["size"] or 0),
                "mtime": float(row["mtime"] or 0.0),
            })

        duplicates = [items for items in groups.values() if len(items) > 1]
        duplicates.sort(key=lambda items: (-len(items), basename(items[0]["path"]).casefold()))
        return [
            {
                "name": basename(items[0]["path"]),
                "kind": "image",
                "items": items[:per_group],
                "keep": _best_copy(items)["file_id"],
            }
            for items in duplicates[:limit]
        ]

    def duplicate_documents(
        self,
        limit: int = 30,
        per_group: int = 500,
        scan_limit: int = 4000,
        progress: Callable[[int, int], None] | None = None,
    ) -> list[dict]:
        """Near-identical documents, grouped by a text fingerprint.

        A copy saved as PDF and as DOCX has different bytes, so the image
        report cannot see it. Each document's first chunks are reduced to a
        64-bit SimHash of three-word shingles; files whose fingerprints differ
        in at most four bits are near-duplicates. SimHash is stable across
        line breaks, headers and reflowing, which is exactly the difference
        between "the same text" and "the same file".

        The scan is newest-first and bounded: a fuzzy pass is a convenience,
        not a reason to walk a 100k-file index on a button press. The optional
        `progress(done, total)` callback fires about 25 times so a caller can
        stream a percentage to the UI while the fingerprints are computed.
        """
        rows = self.conn.execute(
            "SELECT f.id, f.path, f.size, f.mtime, t.hash, t.raw_len, t.block_size, "
            "t.offsets, t.data FROM files f "
            "JOIN file_texts t ON t.hash = f.text_hash "
            "WHERE f.kind != 'image' "
            "ORDER BY f.mtime DESC, f.id LIMIT ?",
            (scan_limit,),
        ).fetchall()
        total = len(rows)
        meta: dict[int, dict] = {}
        hashes: dict[int, int] = {}
        step = max(1, total // 25)
        if progress is not None:
            progress(0, total)
        for done, row in enumerate(rows, 1):
            file_id = int(row["id"])
            meta[file_id] = {
                "file_id": file_id,
                "path": str(row["path"]),
                "size": int(row["size"] or 0),
                "mtime": float(row["mtime"] or 0.0),
            }
            # The fingerprint reads only the head of a document, so only the
            # first block (16 384 chars) is inflated — one block per file, not
            # the whole corpus.
            value = _simhash(_row_slice(row, 0, 8000))
            if value:
                hashes[file_id] = value
            if progress is not None and (done % step == 0 or done == total):
                progress(done, total)
        if len(hashes) < 2:
            return []

        # Four 16-bit bands: two fingerprints within 4 bits differ in at most
        # four bands, so at least one band is identical. Bucketing by band turns
        # the quadratic comparison into a handful of small lists.
        bands: dict[tuple[int, int], list[int]] = {}
        for file_id, value in hashes.items():
            for band in range(4):
                key = (band, (value >> (band * 16)) & 0xFFFF)
                bands.setdefault(key, []).append(file_id)

        parent: dict[int, int] = {file_id: file_id for file_id in hashes}

        def find(node: int) -> int:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        def union(left: int, right: int) -> None:
            root_left, root_right = find(left), find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for members in bands.values():
            if len(members) < 2 or len(members) > 60:
                continue
            for index, left in enumerate(members):
                for right in members[index + 1:]:
                    if (hashes[left] ^ hashes[right]).bit_count() <= 4:
                        union(left, right)

        grouped: dict[int, list[int]] = {}
        for file_id in hashes:
            grouped.setdefault(find(file_id), []).append(file_id)
        groups = [ids for ids in grouped.values() if len(ids) > 1]
        groups.sort(key=len, reverse=True)
        out: list[dict] = []
        for ids in groups[:limit]:
            items = [meta[file_id] for file_id in ids]
            items.sort(key=lambda item: basename(item["path"]).casefold())
            out.append({
                "name": basename(items[0]["path"]),
                "kind": "doc",
                "items": items[:per_group],
                "keep": _best_copy(items, prefer_size=True)["file_id"],
            })
        return out

    def image_meta(self, file_ids: Iterable[int]) -> dict[int, tuple[int, int, int]]:
        """(width, height, phash) per file, in one query.

        The listing path builds its rows from the name index and has no image
        data; fetching it one file at a time would be a query per screenshot.
        """
        ids = [int(file_id) for file_id in file_ids]
        if not ids:
            return {}
        out: dict[int, tuple[int, int, int]] = {}
        for start in range(0, len(ids), 500):
            block = ids[start:start + 500]
            rows = self.conn.execute(
                "SELECT file_id, width, height, phash FROM images WHERE file_id IN "
                f"({','.join('?' * len(block))})",
                block,
            ).fetchall()
            for row in rows:
                out[int(row["file_id"])] = (
                    int(row["width"] or 0), int(row["height"] or 0), int(row["phash"] or 0)
                )
        return out

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
        phash: int = 0,
        ocr_boxes: str | None = None,
    ) -> int:
        self.conn.execute(
            "INSERT INTO images(path, file_id, sha256, width, height, size, mtime, thumb, ocr_text, "
            "ocr_boxes, ocr_engine, phash, indexed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET file_id=excluded.file_id, sha256=excluded.sha256, "
            "width=excluded.width, height=excluded.height, size=excluded.size, mtime=excluded.mtime, "
            "thumb=excluded.thumb, ocr_text=excluded.ocr_text, ocr_engine=excluded.ocr_engine, "
            "ocr_boxes=excluded.ocr_boxes, phash=excluded.phash, indexed_at=excluded.indexed_at",
            (path, file_id, sha256, width, height, size, mtime,
             *self._seal_pair(thumb, ocr_text), ocr_boxes, ocr_engine, int(phash), time.time()),
        )
        row = self.conn.execute("SELECT id FROM images WHERE path = ?", (path,)).fetchone()
        image_id = int(row["id"])
        self.conn.execute("DELETE FROM vectors WHERE owner_type='image' AND owner_id = ?", (image_id,))
        if vector is not None:
            self.conn.execute(
                "INSERT INTO vectors(owner_type, owner_id, dim, vec) VALUES('image',?,?,?)",
                (image_id, int(vector.shape[0]), vecstore.quantize_vector(vector)),
            )
        self._invalidate_vectors()
        return image_id

    def sha256_for(self, file_ids: Iterable[int]) -> dict[int, str]:
        """Content hash per file, in blocks — grouping collapses copies by it."""
        ids = [int(file_id) for file_id in file_ids]
        if not ids:
            return {}
        out: dict[int, str] = {}
        for start in range(0, len(ids), 500):
            block = ids[start:start + 500]
            rows = self.conn.execute(
                "SELECT id, sha256 FROM files WHERE id IN "
                f"({','.join('?' * len(block))})",
                block,
            ).fetchall()
            for row in rows:
                if row["sha256"]:
                    out[int(row["id"])] = str(row["sha256"])
        return out

    def missing_phash(self, limit: int = 100_000) -> list[sqlite3.Row]:
        """Images indexed before `phash` existed, newest first.

        `phash` arrived in schema v4, so an index built by an older version has
        the column and nothing in it. Re-indexing would redo OCR for every
        screenshot; a hash backfill only reopens the file and reads the pixels.
        """
        return self.conn.execute(
            "SELECT i.id, i.path FROM images i LEFT JOIN files f ON f.id = i.file_id "
            "WHERE i.phash IS NULL ORDER BY f.mtime DESC LIMIT ?",
            (int(limit),),
        ).fetchall()

    def set_phash(self, image_id: int, phash: int) -> None:
        self.conn.execute("UPDATE images SET phash = ? WHERE id = ?", (int(phash), int(image_id)))

    def _seal_pair(self, thumb: bytes | None, ocr_text: str | None) -> tuple:
        """Thumb and OCR text go down sealed when paranoid mode is on."""
        if not self.sealed:
            return thumb, ocr_text
        vault = vault_mod.current()
        if vault is None:
            raise vault_mod.VaultError(
                "the index is sealed but the vault key is not loaded"
            )
        sealed_thumb = vault.seal(thumb) if thumb else thumb
        sealed_ocr = vault.seal(ocr_text.encode("utf-8")) if ocr_text else ocr_text
        return sealed_thumb, sealed_ocr

    def _unseal_text(self, value) -> str:
        """OCR text comes back as bytes when sealed, str when legacy."""
        if isinstance(value, (bytes, bytearray)) and vault_mod.Vault.is_sealed(value):
            vault = vault_mod.current()
            if vault is None:
                raise vault_mod.VaultError(
                    "the index is sealed but the vault key is not loaded"
                )
            return vault.open_bytes(bytes(value)).decode("utf-8", "replace")
        return value

    def thumb(self, file_id: int) -> bytes | None:
        row = self.conn.execute("SELECT thumb FROM images WHERE file_id = ?", (file_id,)).fetchone()
        if not row or not row["thumb"]:
            return None
        blob = bytes(row["thumb"])
        if vault_mod.Vault.is_sealed(blob):
            vault = vault_mod.current()
            if vault is None:
                raise vault_mod.VaultError(
                    "the index is sealed but the vault key is not loaded"
                )
            return vault.open_bytes(blob)
        return blob

    def image_count(self) -> int:
        """Cheap guard for the visual channel: an imageless corpus must never
        pay for loading an image/text embedder."""
        return int(self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0])

    def image_rows(self, limit: int = 200, offset: int = 0) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT i.id, i.path, i.width, i.height, i.ocr_text, i.file_id, f.mtime "
            "FROM images i LEFT JOIN files f ON f.id = i.file_id ORDER BY f.mtime DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()

    # --------------------------------------------------------------- search

    _bm25_key: tuple | None = None

    def inram_bm25(self) -> "bm25mod.InRamBM25 | None":
        """The in-RAM BM25 index for this database, or None until ready.

        Off for one-shot CLI searches (`ram_bm25=False`): at 298k chunks the
        build measured 35 s and ~1.5 GB, and the thread slowed the query that
        started it by holding the GIL. The app and MCP servers are long-lived,
        so there the build pays off on multi-word queries, where FTS5 prefix
        expansion costs ~100 ms.

        The first enabled call kicks a background build; until it lands the
        caller falls back to the FTS5 path, so nothing pays the build in the
        foreground. The cache is module-level: the API server opens a
        short-lived Store per request, and the index must outlive any single
        one of them."""
        sealed = self.sealed
        # The sidecar works in BOTH modes: sealed (encrypted) and normal
        # (plaintext). The CLI used to pass ram_bm25=False and silently lose
        # the content channel on indexes without FTS; now the sidecar is
        # always consulted first, and cold CLI pays ~100 ms instead of 4 s.
        if not (self._ram_bm25 or sealed):
            # One-shot CLI: only load an existing sidecar, never kick a build.
            try:
                key = (str(self.path), *self._stamps())
                fp = self._chunks_fingerprint()
            except sqlite3.Error:
                return None
            if _BM25["key"] == key and _BM25["index"] is not None:
                return _BM25["index"]
            side = self._load_sealed_bm25(fp)
            if side is not None:
                _BM25["index"] = side
                _BM25["key"] = key
                return side
            return None
        try:
            # The stamps move on any commit — this process's or the indexer's —
            # and cost two stat() calls. `SELECT COUNT(*), MAX(id) FROM chunks`
            # answered the same question for 3.7 ms on a 100k-chunk index,
            # which was 13% of a warm query.
            key = (str(self.path), *self._stamps())
        except sqlite3.Error:
            return None
        if _BM25["key"] == key and _BM25["index"] is not None:
            return _BM25["index"]
        with _BM25_LOCK:
            if _BM25["key"] == key and _BM25["index"] is not None:
                return _BM25["index"]
            fp = self._chunks_fingerprint()
            if _BM25.get("fp") == fp and _BM25["index"] is not None:
                # Same corpus, files were merely touched: keep serving the
                # index that is already in RAM.
                _BM25["key"] = key
                return _BM25["index"]
            if sealed:
                # Paranoid mode: the sealed sidecar decrypts in ~100 ms, so
                # load it on the calling thread instead of falling back to a
                # path that no longer exists (FTS is dropped under seal).
                index = self._load_sealed_bm25(fp)
                if index is not None:
                    _BM25["index"] = index
                    _BM25["key"] = key
                    _BM25["fp"] = fp
                    return index
            # Rebuild only when the corpus actually changed. The key carries
            # the file stamps, and any commit moves them - including a
            # recent-searches write, which has nothing to do with chunks.
            # Gating on the fingerprint here is what keeps one search write
            # from superseding an in-flight build every keystroke.
            if _BM25["building"] != key and (
                _BM25["index"] is None or _BM25.get("fp") != fp
            ):
                _BM25["building"] = key

                def _build(store_path=str(self.path), build_key=key, build_fp=fp,
                           was_sealed=sealed) -> None:
                    try:
                        conn = sqlite3.connect(f"file:{store_path}?mode=ro", uri=True)
                        conn.row_factory = sqlite3.Row
                        try:
                            # The generator inflates one document at a time, so
                            # the 300 MB of text never sits in RAM as a whole.
                            index = bm25mod.InRamBM25.build(_iter_chunk_texts(conn))
                        finally:
                            conn.close()
                        index.fingerprint = build_fp
                        with _BM25_LOCK:
                            _BM25["index"] = index
                            _BM25["key"] = build_key
                            _BM25["fp"] = build_fp
                        if was_sealed:
                            self._write_sealed_bm25(index)
                    except Exception:
                        # The database went away mid-build; FTS5 stays the
                        # path. Clear the in-flight marker or this corpus
                        # state never retries — the guard below compares
                        # `building` against the key on every call.
                        with _BM25_LOCK:
                            if _BM25.get("building") == build_key:
                                _BM25["building"] = None

                threading.Thread(target=_build, daemon=True, name="af-bm25-build").start()
        return None

    def _chunks_fingerprint(self) -> tuple[int, int]:
        row = self.conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0) FROM chunks").fetchone()
        return (int(row[0]), int(row[1]))

    def _sealed_bm25_path(self):
        from pathlib import Path as _Path

        return _Path(str(self.path) + ".bm25seal")

    def _write_sealed_bm25(self, index) -> None:
        """Serialize the CSR, seal it (paranoid) or save plaintext (normal),
        write `<db>.bm25seal` so the next cold CLI load is ~100 ms."""
        bio = io.BytesIO()
        np.savez(bio, **index.to_arrays())
        raw = bio.getvalue()
        if self.sealed:
            vault = vault_mod.current()
            if vault is None:
                raise vault_mod.VaultError(
                    "the index is sealed but the vault key is not loaded"
                )
            raw = vault.seal(raw)
        self._sealed_bm25_path().write_bytes(raw)

    def _load_sealed_bm25(self, fp):
        path = self._sealed_bm25_path()
        if not path.exists():
            return None
        raw = path.read_bytes()
        if vault_mod.Vault.is_sealed(raw):
            vault = vault_mod.current()
            if vault is None:
                return None
            raw = vault.open_bytes(raw)
        data = np.load(io.BytesIO(raw), allow_pickle=False)
        index = bm25mod.InRamBM25.from_arrays({name: data[name] for name in data.files})
        if index.fingerprint is not None and tuple(index.fingerprint) != tuple(fp):
            return None  # the corpus moved since it was sealed
        return index

    def reseal_bm25(self) -> None:
        """Rebuild the in-RAM BM25 and save the sidecar. Under seal the
        sidecar is encrypted; in normal mode it is plaintext. Called after
        every index run so the next cold CLI load is ~100 ms."""
        index = bm25mod.InRamBM25.build(_iter_chunk_texts(self.conn))
        fp = self._chunks_fingerprint()
        index.fingerprint = fp
        self._write_sealed_bm25(index)
        with _BM25_LOCK:
            _BM25["index"] = index
            _BM25["key"] = (str(self.path), *self._stamps())
            _BM25["fp"] = fp

    def seal_index(self, progress=None) -> None:
        """Encrypt every content blob at rest and drop FTS from disk.

        `progress(done, total, stage)` is called per row for the UI bar."""
        vault = vault_mod.current()
        if vault is None:
            raise vault_mod.VaultError("no vault installed; call vault.enable() first")

        def _tick(done, total, stage):
            if progress:
                progress(done, total, stage)

        # One unit of work is reserved for the reseal: the bar must not read
        # 100% while the search index (BM25) is still being re-encrypted -
        # that rebuild is part of the job, not an afterthought.
        reseal_weight = 1
        rows_total = (
            self.conn.execute("SELECT COUNT(*) FROM file_texts").fetchone()[0]
            + self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            + self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        )
        total = rows_total + reseal_weight
        done = 0
        rows = self.conn.execute("SELECT hash, data FROM file_texts").fetchall()
        for row in rows:
            done += 1
            _tick(done, total, "seal")
            if vault.is_sealed(row["data"]):
                continue
            self.conn.execute(
                "UPDATE file_texts SET data = ? WHERE hash = ?",
                (vault.seal(row["data"]), row["hash"]),
            )
        rows = self.conn.execute("SELECT id, ocr_text, thumb FROM images").fetchall()
        for row in rows:
            done += 1
            _tick(done, total, "seal")
            sets, vals = [], []
            ocr = row["ocr_text"]
            if ocr and not vault.is_sealed(ocr if isinstance(ocr, bytes) else ocr.encode("utf-8")):
                sets.append("ocr_text = ?")
                vals.append(vault.seal(ocr.encode("utf-8")))
            thumb = row["thumb"]
            if thumb and not vault.is_sealed(thumb):
                sets.append("thumb = ?")
                vals.append(vault.seal(thumb))
            if sets:
                vals.append(int(row["id"]))
                self.conn.execute(
                    "UPDATE images SET " + ", ".join(sets) + " WHERE id = ?", vals
                )
        rows = self.conn.execute("SELECT rowid AS _rid, text FROM symbols").fetchall()
        for row in rows:
            done += 1
            _tick(done, total, "seal")
            text = row["text"]
            if isinstance(text, bytes) and vault.is_sealed(text):
                continue
            self.conn.execute(
                "UPDATE symbols SET text = ? WHERE rowid = ?",
                (vault.seal(text.encode("utf-8")), int(row["_rid"])),
            )
        _set_meta(self.conn, "sealed", "1")
        self.conn.commit()
        # No postings on disk: the vocabulary would reconstruct the texts.
        self.conn.execute("DROP TABLE IF EXISTS chunks_fts")
        # `chunks_vocab` is an fts5vocab *table* in v9 (a view in older layouts).
        self.conn.execute("DROP TABLE IF EXISTS chunks_vocab")
        self.conn.execute("DROP VIEW IF EXISTS chunks_vocab")
        self.conn.commit()
        _tick(rows_total, total, "reseal")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")
        self.conn.commit()
        # The nameindex sidecar is every file name and full path in plaintext:
        # sealing the index but keeping the sidecar would defeat the promise.
        # It is a cache — the name index rebuilds from the (sealed) database.
        self._evict_name_sidecars()
        self.reseal_bm25()
        # The search index is re-encrypted: only now the job reads 100%.
        _tick(total, total, "reseal")

    def _evict_name_sidecars(self) -> None:
        """Delete every nameindex sidecar file for this database."""
        base = Path(str(self.path) + ".nameidx")
        for path in [base, *base.parent.glob(base.name + ".*.bin")]:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass  # a locked cache file is removed on the next run
        try:
            from .nameindex import NAME_CACHE
            NAME_CACHE.clear()
        except Exception:
            pass

    def unseal_index(self, progress=None) -> None:
        """Decrypt everything back and rebuild FTS (paranoid mode off)."""
        vault = vault_mod.current()
        if vault is None:
            raise vault_mod.VaultError("no vault installed; the sealed index cannot be read")

        def _tick(done, total, stage):
            if progress:
                progress(done, total, stage)

        fts_weight = 1
        rows_total = (
            self.conn.execute("SELECT COUNT(*) FROM file_texts").fetchone()[0]
            + self.conn.execute("SELECT COUNT(*) FROM images").fetchone()[0]
            + self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
        )
        total = rows_total + fts_weight
        done = 0
        rows = self.conn.execute("SELECT hash, data FROM file_texts").fetchall()
        for row in rows:
            done += 1
            _tick(done, total, "unseal")
            data = row["data"]
            if vault.is_sealed(data):
                self.conn.execute(
                    "UPDATE file_texts SET data = ? WHERE hash = ?",
                    (vault.open_bytes(data), row["hash"]),
                )
        rows = self.conn.execute("SELECT id, ocr_text, thumb FROM images").fetchall()
        for row in rows:
            done += 1
            _tick(done, total, "unseal")
            sets, vals = [], []
            ocr = row["ocr_text"]
            if isinstance(ocr, (bytes, bytearray)) and vault.is_sealed(ocr):
                sets.append("ocr_text = ?")
                vals.append(vault.open_bytes(bytes(ocr)).decode("utf-8", "replace"))
            thumb = row["thumb"]
            if thumb and vault.is_sealed(thumb):
                sets.append("thumb = ?")
                vals.append(vault.open_bytes(bytes(thumb)))
            if sets:
                vals.append(int(row["id"]))
                self.conn.execute(
                    "UPDATE images SET " + ", ".join(sets) + " WHERE id = ?", vals
                )
        rows = self.conn.execute("SELECT rowid AS _rid, text FROM symbols").fetchall()
        for row in rows:
            done += 1
            _tick(done, total, "unseal")
            text = row["text"]
            if isinstance(text, (bytes, bytearray)) and vault.is_sealed(text):
                self.conn.execute(
                    "UPDATE symbols SET text = ? WHERE rowid = ?",
                    (vault.open_bytes(bytes(text)).decode("utf-8", "replace"), int(row["_rid"])),
                )
        _set_meta(self.conn, "sealed", "0")
        self.conn.commit()
        _tick(rows_total, total, "fts")
        _rebuild_fts(self.conn)
        _tick(total, total, "fts")
        self.conn.commit()
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.execute("VACUUM")
        self.conn.commit()

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

    def bm25_typo(self, text: str, limit: int) -> list[tuple[int, float]]:
        """The typo channel, with no index of its own.

        The old tier was a second full copy of the corpus tokenized as
        3-grams — 778 MB here — and it only ever ran when the exact channels
        came back nearly empty. `chunks_vocab` is a view over the term
        dictionary the FTS index already keeps, so a misspelled term can be
        matched to the real one and re-queried for the price of one scan.
        """
        if self.sealed:
            # chunks_vocab rides chunks_fts, which paranoid mode drops from
            # disk; the in-RAM BM25 prefix scan covers near-terms instead.
            return []
        terms = [token for token in bm25mod.TOKEN_RE.findall(text) if len(token) >= 4]
        corrected = []
        for term in terms:
            fixed = self._correct_term(term.lower())
            if fixed:
                corrected.append(fixed)
        if not corrected:
            return []
        query = " OR ".join(f'"{term}"*' for term in corrected if '"' not in term)
        return self.bm25(query, limit) if query else []

    def did_you_mean(self, text: str) -> str | None:
        """A corrected version of `text`, or None when every term is known.

        Used for the zero-result screen: "did you mean ..." comes from the
        same vocabulary the typo channel fixes with, so the suggestion is a
        query that can actually match."""
        terms = bm25mod.TOKEN_RE.findall(text)
        if not terms:
            return None
        changed = False
        out = []
        for term in terms:
            if len(term) >= 4 and self.bm25(f'"{term}"', limit=1) == []:
                fixed = self._correct_term(term.lower())
                if fixed and fixed != term.lower():
                    out.append(fixed)
                    changed = True
                    continue
            out.append(term)
        if not changed:
            return None
        return " ".join(out)

    def _correct_term(self, term: str) -> str | None:
        """The closest vocabulary term to `term`, or None if it is spelled right.

        A 2-character prefix range keeps the candidate list small: the words
        that could be one or two edits away almost always start the same way,
        and a full-vocabulary pass would cost more than the query it fixes.
        """
        if len(term) < 4 or not term[:2].isalpha():
            return None
        prefix = term[:2]
        try:
            rows = self.conn.execute(
                "SELECT term FROM chunks_vocab WHERE term >= ? AND term < ? LIMIT 4000",
                (prefix, prefix + "\U0010ffff"),
            ).fetchall()
        except sqlite3.Error:
            return None
        if not rows:
            return None
        import difflib

        best: tuple[float, str] | None = None
        for row in rows:
            candidate = str(row["term"])
            if candidate == term:
                return None
            if abs(len(candidate) - len(term)) > 2 or not candidate.isalpha():
                continue
            ratio = difflib.SequenceMatcher(None, term, candidate).ratio()
            if ratio >= 0.8 and (best is None or ratio > best[0]):
                best = (ratio, candidate)
        return best[1] if best else None

    def packed_index(self) -> tuple[np.ndarray, np.ndarray]:
        """Every file's packed sign bits as one (rows, bytes) uint8 array,
        shared process-wide. `knn` scores these rows through per-byte lookup
        tables, so the ±1 float32 matrix (154 MB at 100k files) never needs
        to exist in RAM."""
        if self.cache is not None:
            return self.cache.get(self)
        return self._load_vectors()

    @property
    def vector_backend(self) -> str:
        return self._vec_backend

    @property
    def vector_is_file_level(self) -> bool:
        """True when `knn` returns file ids instead of chunk ids."""
        return self._vec_backend == "filebin"

    def _load_vectors(self) -> tuple[np.ndarray, np.ndarray]:
        # File stamps, not `SELECT COUNT(*), MAX(file_id)`: on a 100k-file
        # index that count is a 3 ms scan paid on every query, which was 11%
        # of a warm search. Two stat() calls answer the same question, and
        # `_vec_gen` covers this connection's own writes. The cache is
        # module-level: this Store dies with the request, the matrix must not.
        key = (str(self.path), *self._stamps(), self._vec_gen)
        cached = _FILEVEC_CACHE
        if cached["key"] == key and cached["bits"] is not None:
            return cached["ids"], cached["bits"]
        with _FILEVEC_LOCK:
            if _FILEVEC_CACHE["key"] == key and _FILEVEC_CACHE["bits"] is not None:
                return _FILEVEC_CACHE["ids"], _FILEVEC_CACHE["bits"]
            rows = self.conn.execute(
                "SELECT file_id, bits FROM file_vectors WHERE dim = ? ORDER BY file_id",
                (int(self.dim),),
            ).fetchall()
            if not rows:
                ids = np.zeros(0, dtype=np.int64)
                bits = np.zeros((0, 0), dtype=np.uint8)
            else:
                # `np.fromiter` skips the intermediate 100k-element Python list
                # `np.array([int(...) ...])` builds; ~2 ms of a cold load.
                ids = np.fromiter(
                    (int(row[0]) for row in rows), dtype=np.int64, count=len(rows)
                )
                bits = np.frombuffer(
                    b"".join(row[1] for row in rows), dtype=np.uint8
                ).reshape(len(rows), -1)
            _FILEVEC_CACHE["key"] = key
            _FILEVEC_CACHE["ids"] = ids
            _FILEVEC_CACHE["bits"] = bits
            self._vec_key = key
            return ids, bits

    def knn(self, query_vector: np.ndarray, limit: int) -> list[tuple[int, float]]:
        """Nearest files to a query vector.

        Stored rows are ±1 sign patterns packed 48 bytes per file; the score
        is the same asymmetric dot the float32 matrix computed, assembled from
        per-byte lookup tables — the matrix itself never exists in RAM."""
        ids, bits = self.packed_index()
        if not len(ids) or bits.size == 0:
            return []
        if bits.shape[1] * 8 < int(query_vector.shape[0]):
            return []
        scores = filevec.lut_scores(bits, query_vector)
        take = min(limit, len(ids))
        top = np.argpartition(-scores, take - 1)[:take]
        top = top[np.argsort(-scores[top])]
        return [(int(ids[i]), float(scores[i])) for i in top]

    def pack(self):
        """The memory-mapped matrix, shared process-wide and kept open.

        Freshness is decided by file stamps in the pack cache; the (rows, max
        id) key is only counted when a stamp actually moved. Before the cache
        every query paid a `SELECT COUNT(*)` over the vectors table plus an
        mmap teardown in `close()` — profiled at ~70 ms of a 110 ms query. The
        returned pack is checked out: `close()` releases it, and until then the
        cache will not unmap it under us.

        The previous checkout is released before the acquire: if this very pack
        has to be rebuilt right now, a mapping we still hold would block the
        file rewrite on Windows and the rebuild would silently serve the stale
        file.
        """
        if self._checkout is not None:
            vecstore.PACKS.release(self.path, self._checkout)
            self._checkout = None
        pack = vecstore.PACKS.acquire(self.path, self.dim, self.conn)
        self._checkout = pack
        return pack

    def release_pack(self) -> None:
        """Give the pack checkout back early.

        A resident reader keeps its checkout only while it is actively
        searching; holding it forever would turn the pack into an unclosable
        zombie at rebuild time and block the file replacement on Windows.
        """
        if self._checkout is not None:
            vecstore.PACKS.release(self.path, self._checkout)
            self._checkout = None

    def _close_pack(self) -> None:
        # The pack is shared by every Store on this database, so nothing is
        # unmapped here; dropping the cache entry just forces the next `pack()`
        # to revalidate against the table.
        vecstore.PACKS.drop(self.path)
        if self._checkout is not None:
            vecstore.PACKS.release(self.path, self._checkout)
            self._checkout = None

    def rebuild_pack(self):
        """Refresh the pack after an index run. Incremental: new vectors land
        in the delta layer and the base file is left alone; deleted chunk rows
        (bumped `vector_epoch`) or a delta past its cap fall back to the full
        rebuild. Called at the end of a run so the first search does not pay
        for it. The Store's own checkout comes through the standard `acquire`
        path — `release` must never see a pack that no `acquire` balanced."""
        vecstore.PACKS.refresh(self.path, self.conn, self.dim)
        return self.pack()

    def image_index(self, dim: int | None = None) -> tuple[np.ndarray, np.ndarray]:
        sql = "SELECT owner_id AS id, dim, vec FROM vectors WHERE owner_type='image'"
        params: tuple = ()
        if dim is not None:
            sql += " AND dim = ?"
            params = (dim,)
        rows = self.conn.execute(sql + " ORDER BY owner_id", params).fetchall()
        if not rows:
            return np.zeros(0, dtype=np.int64), np.zeros((0, dim or 1), dtype=np.float32)
        ids = np.array([row["id"] for row in rows], dtype=np.int64)
        matrix = np.vstack(
            [vecstore.dequantize_vector(row["vec"], int(row["dim"]))
             for row in rows]
        ).astype(np.float32)
        return ids, matrix

    def knn_images(self, query_vector: np.ndarray, limit: int) -> list[tuple[int, float]]:
        ids, matrix = self.image_index(dim=int(query_vector.shape[0]))
        return _top_k(ids, matrix, query_vector, limit)

    def chunk_rows(self, ids: Iterable[int]) -> dict[int, dict]:
        """Chunk metadata plus its text slice, keyed by chunk id.

        The text is cut from the file's stored blocks: a chunk is ~1 200 chars
        inside a 16 384-char block, so a query inflates one block per hit
        instead of the whole document.
        """
        ids = list(ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT c.id, c.ord, c.start, c.len, f.id AS file_id, f.path, f.kind, "
            f"f.size, f.mtime, f.text_hash "
            f"FROM chunks c JOIN files f ON f.id = c.file_id WHERE c.id IN ({placeholders})",
            ids,
        ).fetchall()
        texts = self.text_rows({row["text_hash"] for row in rows})
        out: dict[int, dict] = {}
        for row in rows:
            stored = texts.get(row["text_hash"] or "")
            start = int(row["start"])
            out[int(row["id"])] = {
                "id": int(row["id"]),
                "ord": int(row["ord"]),
                "text": _row_slice(stored, start, start + int(row["len"])) if stored else "",
                "file_id": int(row["file_id"]),
                "path": row["path"],
                "kind": row["kind"],
                "size": int(row["size"]),
                "mtime": float(row["mtime"]),
            }
        return out

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

    def resolve_paths(self, paths: Sequence[str]) -> dict[str, int]:
        """Map as-written paths to file ids, ignoring slash direction and case.

        Used by the kensaku import: their config stores absolute Windows paths
        with backslashes, and a path that differs only in case is the same file.
        One full scan of the files table is fine here — an import runs once, and
        100k short strings is a fraction of a second.
        """
        wanted = {path: normalize_path(path) for path in paths}
        if not wanted:
            return {}
        index = {
            normalize_path(row["path"]): int(row["id"])
            for row in self.conn.execute("SELECT id, path FROM files").fetchall()
        }
        return {path: index[key] for path, key in wanted.items() if key in index}

    def file_rows_by_ids(self, ids: Iterable[int]) -> list[sqlite3.Row]:
        ids = list(ids)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        return self.conn.execute(
            f"SELECT id, path, name, kind, size, mtime FROM files WHERE id IN ({placeholders})",
            ids,
        ).fetchall()

    def regex_chunks(self, pattern: str, limit: int) -> list[dict]:
        """Bounded regex scan over the stored document text.

        There is no `chunks.text` to ask SQLite about any more, so the scan
        inflates each file's text once and maps every match position back to the
        chunk that contains it. That is one regex pass over the corpus rather
        than one per chunk, which is what keeps a `re:` query in the same
        ballpark as the old full-table scan. It only runs for an explicit `re:`
        query, and the limit bounds the damage.
        """
        try:
            rx = re.compile(pattern)
        except re.error:
            return []
        cursor = self.conn.execute(
            "SELECT c.id, c.ord, c.start, c.len, f.id AS file_id, f.path, f.kind, "
            "f.size, f.mtime, f.text_hash "
            "FROM chunks c JOIN files f ON f.id = c.file_id ORDER BY c.file_id, c.ord"
        )
        # Load every text row up front in a handful of queries: asking per file
        # would add 12 000 round trips (~0.6 s) to a scan that is already the
        # most expensive query in the app.
        stored = self.text_rows(
            row[0] for row in self.conn.execute(
                "SELECT text_hash FROM files WHERE text_hash IS NOT NULL"
            )
        )
        out: list[dict] = []
        for _, rows in groupby(cursor, key=lambda row: int(row["file_id"])):
            rows = list(rows)
            stored_row = stored.get(rows[0]["text_hash"] or "")
            text = _row_text(stored_row) if stored_row else ""
            if not text:
                continue
            starts = [int(row["start"]) for row in rows]
            seen: set[int] = set()
            for match in rx.finditer(text):
                position = match.start()
                index = bisect_right(starts, position) - 1
                if index < 0:
                    index = 0
                row = rows[index]
                start = int(row["start"])
                if position >= start + int(row["len"]):
                    continue  # the match fell in the gap between chunks
                chunk_id = int(row["id"])
                if chunk_id in seen:
                    continue
                seen.add(chunk_id)
                out.append({
                    "id": chunk_id,
                    "ord": int(row["ord"]),
                    "text": text[start:start + int(row["len"])],
                    "file_id": int(row["file_id"]),
                    "path": row["path"],
                    "kind": row["kind"],
                    "size": int(row["size"]),
                    "mtime": float(row["mtime"]),
                })
                if len(out) >= limit:
                    return out
        return out

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

    # ------------------------------------------------------------- personal

    def pinned_ids(self) -> set[int]:
        try:
            rows = self.conn.execute("SELECT file_id FROM pins").fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return set()
        return {int(row["file_id"]) for row in rows}

    def pinned_rows(self, limit: int = 24) -> list[sqlite3.Row]:
        """Pinned files, most recently pinned first, with the file row attached."""
        try:
            return self.conn.execute(
                "SELECT f.id, f.path, f.name, f.kind, f.size, f.mtime, p.added_at "
                "FROM pins p JOIN files f ON f.id = p.file_id "
                "ORDER BY p.added_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return []

    def set_pin(self, file_id: int, pinned: bool) -> bool:
        if pinned:
            self.conn.execute(
                "INSERT INTO pins(file_id, added_at) VALUES(?, ?) "
                "ON CONFLICT(file_id) DO NOTHING",
                (file_id, time.time()),
            )
        else:
            self.conn.execute("DELETE FROM pins WHERE file_id = ?", (file_id,))
        return pinned

    def saved_searches(self, limit: int = 40) -> list[sqlite3.Row]:
        try:
            return self.conn.execute(
                "SELECT id, query, label, created, used, last_used FROM saved "
                "ORDER BY used DESC, last_used DESC, created DESC LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return []

    def save_search(self, query: str, label: str = "") -> int:
        """Store a query once. Saving the same query twice updates the label."""
        now = time.time()
        self.conn.execute(
            "INSERT INTO saved(query, label, created) VALUES(?, ?, ?) "
            "ON CONFLICT(query) DO UPDATE SET label = excluded.label",
            (query, label, now),
        )
        row = self.conn.execute("SELECT id FROM saved WHERE query = ?", (query,)).fetchone()
        return int(row["id"]) if row else 0

    def drop_search(self, saved_id: int) -> bool:
        cursor = self.conn.execute("DELETE FROM saved WHERE id = ?", (saved_id,))
        return cursor.rowcount > 0

    def touch_search(self, query: str) -> None:
        self.conn.execute(
            "UPDATE saved SET used = used + 1, last_used = ? WHERE query = ?",
            (time.time(), query),
        )

    def aliases(self) -> dict[str, str]:
        try:
            rows = self.conn.execute("SELECT alias, expand FROM aliases").fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return {}
        return {str(row["alias"]): str(row["expand"]) for row in rows}

    def set_alias(self, alias: str, expand: str) -> None:
        self.conn.execute(
            "INSERT INTO aliases(alias, expand) VALUES(?, ?) "
            "ON CONFLICT(alias) DO UPDATE SET expand = excluded.expand",
            (alias, expand),
        )

    def drop_alias(self, alias: str) -> bool:
        cursor = self.conn.execute("DELETE FROM aliases WHERE alias = ?", (alias,))
        return cursor.rowcount > 0

    # ---------------------------------------------------------------- tags

    def set_note(self, file_id: int, text: str | None) -> bool:
        """Write (or clear, with None/empty) the human note on a file."""
        clean = " ".join((text or "").split())[:2000]
        if clean:
            self.conn.execute(
                "INSERT INTO notes(file_id, text, added_at) VALUES (?, ?, ?) "
                "ON CONFLICT(file_id) DO UPDATE SET text = excluded.text, "
                "added_at = excluded.added_at",
                (int(file_id), clean, time.time()),
            )
        else:
            self.conn.execute("DELETE FROM notes WHERE file_id = ?", (int(file_id),))
        self.conn.commit()
        return bool(clean)

    def notes_for(self, file_ids: Iterable[int]) -> dict[int, str]:
        ids = [int(fid) for fid in file_ids]
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT file_id, text FROM notes WHERE file_id IN ({marks})", ids
        ).fetchall()
        return {int(row[0]): str(row[1]) for row in rows}

    def tag_colors(self) -> dict[str, str]:
        return {
            str(row[0]): str(row[1])
            for row in self.conn.execute("SELECT tag, color FROM tag_colors")
        }

    def set_tag_color(self, tag: str, color: str | None) -> bool:
        """ colour = '#rrggbb'; None removes the custom colour. """
        clean = " ".join(str(tag or "").split())[:64]
        if not clean:
            return False
        if color is None or color == "":
            # no colour means "remove the custom colour"
            self.conn.execute("DELETE FROM tag_colors WHERE tag = ?", (clean,))
            self.conn.commit()
            return True
        if not (len(color) == 7 and color.startswith("#")):
            return False  # a bad literal is rejected, never stored
        self.conn.execute(
            "INSERT INTO tag_colors(tag, color) VALUES (?, ?) "
            "ON CONFLICT(tag) DO UPDATE SET color = excluded.color",
            (clean, color),
        )
        self.conn.commit()
        return True

    def tags_for(self, file_ids: Iterable[int]) -> dict[int, list[str]]:
        """Tags per file, in one query — a page of results must not cost N."""
        ids = [int(file_id) for file_id in file_ids]
        if not ids:
            return {}
        out: dict[int, list[str]] = {}
        for start in range(0, len(ids), 400):
            block = ids[start:start + 400]
            try:
                rows = self.conn.execute(
                    "SELECT file_id, tag FROM tags WHERE file_id IN "
                    f"({','.join('?' * len(block))}) ORDER BY lower(tag)",
                    block,
                ).fetchall()
            except sqlite3.OperationalError as exc:
                _reraises_unless_missing(exc)
                return {}
            for row in rows:
                out.setdefault(int(row["file_id"]), []).append(str(row["tag"]))
        return out

    def all_tags(self, limit: int = 200) -> list[dict]:
        try:
            rows = self.conn.execute(
                "SELECT MIN(tag) AS tag, COUNT(*) AS n FROM tags "
                "GROUP BY lower(tag) ORDER BY n DESC, lower(tag) LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return []
        return [{"tag": str(row["tag"]), "count": int(row["n"])} for row in rows]

    def set_tag(self, file_id: int, tag: str, on: bool = True) -> bool:
        clean = " ".join(str(tag or "").split())[:64]
        if not clean:
            return False
        if on:
            self.conn.execute(
                "INSERT INTO tags(file_id, tag, added_at) VALUES(?, ?, ?) "
                "ON CONFLICT(file_id, tag) DO NOTHING",
                (int(file_id), clean, time.time()),
            )
        else:
            self.conn.execute(
                "DELETE FROM tags WHERE file_id = ? AND lower(tag) = lower(?)",
                (int(file_id), clean),
            )
        return on

    def set_tags(self, file_ids: Iterable[int], tag: str, on: bool = True) -> int:
        changed = 0
        for file_id in {int(item) for item in file_ids}:
            self.set_tag(file_id, tag, on)
            changed += 1
        return changed

    def ocr_scope(self, terms: Sequence[str]) -> set[int]:
        """Files whose images' OCR text carries every term (AND).

        `ocr:invoice` is the answer to "the screenshot where the word invoice
        appears" — a question the name and the pixels' caption answer together.
        Case-insensitive LIKE over the OCR column: images are the smallest
        table here, and the terms are short.
        """
        clean = [str(term or "").strip().lower()[:120] for term in terms]
        clean = [term for term in clean if term]
        if not clean:
            return set()
        rows = self.conn.execute(
            "SELECT i.file_id, i.ocr_text FROM images i "
            "WHERE i.file_id IS NOT NULL AND i.ocr_text IS NOT NULL"
        ).fetchall()
        scope: set[int] = set()
        for row in rows:
            text = (self._unseal_text(row[1]) or "").lower()
            if all(term in text for term in clean):
                scope.add(int(row[0]))
        return scope

    def email_scope(self, from_values: Sequence[str], subject_values: Sequence[str]) -> set[int]:
        """Files whose mail headers match `from:` / `subject:` (each AND).

        Headers are part of the extracted text, so this decompresses only the
        email corpus — a small fraction of any index — and matches the header
        line case-insensitively.
        """
        froms = [str(v or "").strip().lower()[:200] for v in from_values]
        subjects = [str(v or "").strip().lower()[:200] for v in subject_values]
        froms = [v for v in froms if v]
        subjects = [v for v in subjects if v]
        if not froms and not subjects:
            return set()
        rows = self.conn.execute(
            "SELECT id FROM files WHERE kind = 'email'"
        ).fetchall()
        scope: set[int] = set()
        for row in rows:
            text = (self.file_text(int(row[0])) or "").lower()
            headers_from = []
            headers_subj = []
            for line in text.splitlines():
                if line.lower().startswith("from:"):
                    headers_from.append(line[5:].strip())
                elif line.lower().startswith("subject:"):
                    headers_subj.append(line[8:].strip())
            if all(any(v in header for header in headers_from) for v in froms)                     and all(any(v in header for header in headers_subj) for v in subjects):
                scope.add(int(row[0]))
        return scope

    def file_ids_for_tags(self, tags: Sequence[str]) -> set[int]:
        """Files carrying every one of these tags (AND, not OR)."""
        clean = [" ".join(str(tag or "").split())[:64] for tag in tags]
        clean = [tag for tag in clean if tag]
        if not clean:
            return set()
        marks = ",".join("?" * len(clean))
        try:
            rows = self.conn.execute(
                "SELECT file_id FROM tags WHERE lower(tag) IN "
                f"({marks}) GROUP BY file_id HAVING COUNT(DISTINCT lower(tag)) = ?",
                [*(tag.lower() for tag in clean), len({tag.lower() for tag in clean})],
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return set()
        return {int(row["file_id"]) for row in rows}

    # ---------------------------------------------------------- collections

    def collections(self) -> list[dict]:
        try:
            rows = self.conn.execute(
                "SELECT c.id, c.name, c.created, COUNT(cf.file_id) AS n "
                "FROM collections c LEFT JOIN collection_files cf "
                "ON cf.collection_id = c.id GROUP BY c.id "
                "ORDER BY lower(c.name)"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return []
        return [
            {
                "id": int(row["id"]),
                "name": str(row["name"]),
                "created": float(row["created"] or 0.0),
                "count": int(row["n"] or 0),
            }
            for row in rows
        ]

    def collection_by_name(self, name: str) -> sqlite3.Row | None:
        clean = " ".join(str(name or "").split())[:96]
        if not clean:
            return None
        try:
            return self.conn.execute(
                "SELECT id, name, created FROM collections WHERE name = ? COLLATE NOCASE",
                (clean,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return None

    def create_collection(self, name: str) -> int:
        clean = " ".join(str(name or "").split())[:96]
        if not clean:
            return 0
        existing = self.collection_by_name(clean)
        if existing is not None:
            return int(existing["id"])
        self.conn.execute(
            "INSERT INTO collections(name, created) VALUES(?, ?)",
            (clean, time.time()),
        )
        row = self.collection_by_name(clean)
        return int(row["id"]) if row else 0

    def rename_collection(self, collection_id: int, name: str) -> bool:
        clean = " ".join(str(name or "").split())[:96]
        if not clean:
            return False
        cursor = self.conn.execute(
            "UPDATE collections SET name = ? WHERE id = ?", (clean, int(collection_id))
        )
        return cursor.rowcount > 0

    def drop_collection(self, collection_id: int) -> bool:
        self.conn.execute(
            "DELETE FROM collection_files WHERE collection_id = ?", (int(collection_id),)
        )
        cursor = self.conn.execute(
            "DELETE FROM collections WHERE id = ?", (int(collection_id),)
        )
        return cursor.rowcount > 0

    def collection_ids(self, collection_id: int) -> set[int]:
        try:
            rows = self.conn.execute(
                "SELECT file_id FROM collection_files WHERE collection_id = ?",
                (int(collection_id),),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return set()
        return {int(row["file_id"]) for row in rows}

    def collection_ids_for_names(self, names: Sequence[str]) -> set[int]:
        """Files in any of these collections (union: a collection is a place)."""
        clean = [" ".join(str(name or "").split())[:96] for name in names]
        clean = [name for name in clean if name]
        if not clean:
            return set()
        marks = ",".join("?" * len(clean))
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT cf.file_id FROM collection_files cf "
                "JOIN collections c ON c.id = cf.collection_id "
                f"WHERE c.name COLLATE NOCASE IN ({marks})",
                clean,
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return set()
        return {int(row["file_id"]) for row in rows}

    def add_to_collection(self, collection_id: int, file_ids: Iterable[int]) -> int:
        now = time.time()
        rows = [(int(collection_id), int(file_id), now) for file_id in {int(x) for x in file_ids}]
        if not rows:
            return 0
        self.conn.executemany(
            "INSERT INTO collection_files(collection_id, file_id, added_at) "
            "VALUES(?, ?, ?) ON CONFLICT(collection_id, file_id) DO NOTHING",
            rows,
        )
        return len(rows)

    def remove_from_collection(self, collection_id: int, file_ids: Iterable[int]) -> int:
        ids = [int(item) for item in {int(x) for x in file_ids}]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cursor = self.conn.execute(
            f"DELETE FROM collection_files WHERE collection_id = ? AND file_id IN ({marks})",
            [int(collection_id), *ids],
        )
        return max(0, cursor.rowcount)

    # -------------------------------------------------------------- symbols

    def replace_symbols(self, file_id: int, symbols: Sequence[tuple[str, str, int, str]]) -> int:
        """Swap a file's definitions for a fresh set. Rows are (name, kind, line, text)."""
        self.conn.execute("DELETE FROM symbols WHERE file_id = ?", (file_id,))
        if not symbols:
            return 0
        sealed = self.sealed
        vault = vault_mod.current() if sealed else None
        if sealed and vault is None:
            raise vault_mod.VaultError(
                "the index is sealed but the vault key is not loaded"
            )
        self.conn.executemany(
            "INSERT INTO symbols(file_id, name, lowered, kind, line, text) VALUES(?, ?, ?, ?, ?, ?)",
            [
                (file_id, name, name.lower(), kind, line,
                 vault.seal(text.encode("utf-8")) if vault else text)
                for name, kind, line, text in symbols
            ],
        )
        return len(symbols)

    def find_symbols(self, name: str, limit: int = 40, offset: int = 0) -> list[sqlite3.Row]:
        """Exact name first, then prefix, then substring — one query, one order.

        A symbol search is almost always a name you half-remember, so the
        ordering has to put `parse_query` above `_parse_query_legacy` above
        everything that merely contains the letters. `offset` pages through
        that fixed order for MCP clients that show one page at a time.
        """
        needle = name.strip().lower()
        if not needle:
            return []
        like = f"%{needle}%"
        try:
            rows = self.conn.execute(
                "SELECT s.id, s.name, s.kind, s.line, s.text, "
                "       f.id AS file_id, f.path, f.name AS file_name, f.kind AS file_kind "
                "FROM symbols s JOIN files f ON f.id = s.file_id "
                "WHERE s.lowered LIKE ? "
                "ORDER BY CASE "
                "  WHEN s.lowered = ? THEN 0 "
                "  WHEN s.lowered LIKE ? THEN 1 "
                "  ELSE 2 END, length(s.name), f.path, s.line "
                "LIMIT ? OFFSET ?",
                (like, needle, f"{needle}%", limit, max(0, int(offset))),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return []
        out = []
        for row in rows:
            entry = dict(row)
            entry["text"] = self._unseal_text(row["text"])
            out.append(entry)
        return out

    def symbol_count(self) -> int:
        try:
            return int(self.conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return 0

    def files_without_symbols(self, limit: int = 100_000) -> list[sqlite3.Row]:
        """Code files indexed before the symbols table existed."""
        try:
            return self.conn.execute(
                "SELECT id, path, kind FROM files "
                "WHERE kind = 'code' AND id NOT IN (SELECT file_id FROM symbols) "
                "LIMIT ?",
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            _reraises_unless_missing(exc)
            return []

    def stats(self) -> dict[str, int]:
        def scalar(sql: str) -> int:
            return int(self.conn.execute(sql).fetchone()[0])

        texts = scalar("SELECT COUNT(*) FROM file_texts")
        return {
            "files": scalar("SELECT COUNT(*) FROM files"),
            "chunks": scalar("SELECT COUNT(*) FROM chunks"),
            "vectors": scalar("SELECT COUNT(*) FROM file_vectors"),
            "images": scalar("SELECT COUNT(*) FROM images"),
            "ocr_images": scalar("SELECT COUNT(*) FROM images WHERE ocr_text IS NOT NULL AND ocr_text != ''"),
            # document texts stored once per content hash; a sealed row is one
            # whose blob carries the AFS1 prefix
            "texts": texts,
            "texts_sealed": scalar(
                "SELECT COUNT(*) FROM file_texts WHERE substr(data, 1, 4) = 'AFS1'"
            ) if self.sealed else 0,
            "symbols": scalar("SELECT COUNT(*) FROM symbols"),
            "db_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "recent_searches": [
                {"q": r["q"], "at": r["at"]}
                for r in self.conn.execute(
                    "SELECT q, at FROM recent_searches ORDER BY at DESC LIMIT 10"
                ).fetchall()
            ] if self._table_exists("recent_searches") else [],
        }

    def _table_exists(self, name: str) -> bool:
        try:
            row = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
            return row is not None
        except sqlite3.Error:
            return False

    def _invalidate_vectors(self) -> None:
        self._vec_gen += 1
        self._vec_key = None
        self._pack_key = None
        self._pack = None

    def index_stamp(self) -> tuple[int, int]:
        """Cheap token that changes whenever the index file is written."""
        return self._stamps()

    def _stamps(self) -> tuple[int, int]:
        """(main file mtime_ns, -wal mtime_ns) — cheap proof nobody wrote."""
        stamps = []
        for candidate in (self.path, Path(str(self.path) + "-wal")):
            try:
                stamps.append(candidate.stat().st_mtime_ns)
            except OSError:
                stamps.append(0)
        return (stamps[0], stamps[1])

    def touch_check(self) -> None:
        """Drop the vector cache when another connection wrote to the file.

        A long-lived reader (the UI server) would otherwise keep serving the
        matrix it loaded at startup. WAL writes land in -wal first, so the main
        file's mtime alone is not enough.
        """
        key = self._stamps()
        if key != self._file_key:
            self._file_key = key
            self._invalidate_vectors()

    def close(self) -> None:
        # The pack, if one was checked out, goes back to the process-wide cache
        # (and closes only when this Store was its last user): unmapping it
        # here re-created it on the next request, ~55 ms on Windows per query.
        if self._checkout is not None:
            vecstore.PACKS.release(self.path, self._checkout)
            self._checkout = None
        vecstore.PACKS.unref(self.path)
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
