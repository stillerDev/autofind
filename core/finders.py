"""Corpus questions that are not text queries: biggest, oldest, duplicated,
dead and most-used files.

kensaku's answer to all five is "grep the filesystem again". The index already
holds size, mtime, sha256 and the open counter — asking it is one SQL query
per question, and every answer respects privacy filters because the rows come
from the index, not from a fresh walk.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass


@dataclass(slots=True)
class Found:
    path: str
    name: str
    kind: str
    size: int
    mtime: float
    score: int | float = 0

    def as_row(self) -> tuple:
        return (self.name, self.path, self.kind, self.size, self.score)


def _rows(conn: sqlite3.Connection, sql: str, args: tuple = (), limit: int = 50) -> list[Found]:
    rows = conn.execute(sql + " LIMIT ?", (*args, limit)).fetchall()
    return [Found(path=r[0], name=r[1], kind=r[2], size=int(r[3] or 0),
                  mtime=float(r[4] or 0), score=r[5]) for r in rows]


def biggest(conn: sqlite3.Connection, min_mb: float = 0, limit: int = 50) -> list[Found]:
    floor = int(min_mb * 1048576)
    return _rows(
        conn,
        "SELECT path, name, kind, size, mtime, size AS score FROM files "
        "WHERE size >= ? ORDER BY size DESC",
        (floor,), limit,
    )


def stale(conn: sqlite3.Connection, days: float, limit: int = 50) -> list[Found]:
    import time as _time
    horizon = _time.time() - days * 86400
    return _rows(
        conn,
        "SELECT path, name, kind, size, mtime, CAST(mtime AS INT) AS score FROM files "
        "WHERE mtime <= ? ORDER BY mtime ASC",
        (horizon,), limit,
    )


def duplicates(conn: sqlite3.Connection, limit: int = 50) -> list[Found]:
    """Same sha256 in two or more files: identical bytes, wherever they hide."""
    return _rows(
        conn,
        "SELECT f.path, f.name, f.kind, f.size, f.mtime, d.copies AS score "
        "FROM files f JOIN ("
        "  SELECT sha256, COUNT(*) AS copies FROM files WHERE sha256 IS NOT NULL"
        "  GROUP BY sha256 HAVING COUNT(*) > 1"
        ") d ON d.sha256 = f.sha256 ORDER BY d.copies DESC, f.size DESC",
        (), limit,
    )


def dead_paths(conn: sqlite3.Connection, limit: int = 100000) -> list[Found]:
    """Indexed paths that vanished from disk. Score is meaningless here; the
    caller decides whether to just list them or prune."""
    rows = conn.execute(
        "SELECT path, name, kind, size, mtime, 0 FROM files"
    ).fetchall()
    found = []
    for r in rows:
        if not os.path.exists(r[0]):
            found.append(Found(path=r[0], name=r[1], kind=r[2], size=int(r[3] or 0),
                               mtime=float(r[4] or 0)))
        if len(found) >= limit:
            break
    return found


def most_opened(conn: sqlite3.Connection, limit: int = 50) -> list[Found]:
    """The personal layer: what you actually open, not what merely matches."""
    return _rows(
        conn,
        "SELECT f.path, f.name, f.kind, f.size, f.mtime, u.opens AS score "
        "FROM files f JOIN usage u ON u.file_id = f.id "
        "WHERE u.opens > 0 ORDER BY u.opens DESC, u.last_open DESC",
        (), limit,
    )


def prune_dead(store, found: list[Found]) -> int:
    """Drop dead paths and their chunks/images from the index."""
    removed = 0
    for item in found:
        row = store.conn.execute("SELECT id FROM files WHERE path = ?", (item.path,)).fetchone()
        if row is None:
            continue
        file_id = int(row[0])
        store.conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        store.conn.execute("DELETE FROM images WHERE file_id = ?", (file_id,))
        store.conn.execute("DELETE FROM files WHERE id = ?", (file_id,))
        removed += 1
    store.conn.commit()
    return removed
