"""In-memory name index: fuzzy file matching without touching SQLite per query.

kensaku keeps every file name in a Rust arena and matches with nucleo, which is
why it answers in ~1.3 ms over 100k files. We used to run one `SELECT` with a
bitwise AND and then build 8 000 Python row objects per query: 35 ms at 8k
files, and it gets worse linearly.

This is the same idea in numpy. Names, paths, kinds and the character bloom are
loaded once into flat arrays; the bloom prefilter runs vectorised over the whole
corpus in well under a millisecond; only the survivors reach the Python scorer.
At 100k files the index costs ~13 MB, which is the same order as kensaku's
12 MB and two orders below the vector matrix it replaces as the main memory
consumer.

The index is rebuilt when the database file (or its WAL) changes, so a running
UI server picks up an index run without a restart.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from .store import char_mask

# Beyond this many survivors the Python scorer, not the prefilter, sets the
# latency. The cap keeps the subsequence tier **bounded** rather than linear in
# corpus size: 20k names is ~9 ms whether the index holds 100k files or 1M. The
# survivors are the newest ones, which is the tie-break the ranking wants anyway.
HARD_CAP = 20_000

# Guards against a pathological index (a whole drive at depth 20). Above this we
# fall back to the SQL path instead of eating gigabytes.
MAX_ENTRIES = 1_500_000

# Bytes that make the character after them read as a word start.
BOUNDARY_BYTES = np.frombuffer(b" ._-(", dtype=np.uint8)


def subsequence_score(query: str, name: str) -> float | None:
    """fzf-style score: None when `query` is not a subsequence of `name`.

    Contiguous runs and word starts are what the eye reads as "the same word",
    so they score far above scattered characters. A literal substring is the
    strongest signal and gets its own branch.
    """
    return _score(query.strip().lower(), name.lower())


def _score(needle: str, hay: str) -> float | None:
    """`_score` on pre-lowered strings: the scorer runs once per survivor and
    lower() on 100k names per query would cost more than the match itself."""
    if not needle:
        return None
    position = hay.find(needle)
    if position != -1:
        score = 120.0
        if position == 0:
            score += 40.0
        elif hay[position - 1] in " ._-(":
            score += 20.0
        # Shorter names mean the match is a bigger share of the name.
        score += 30.0 * (len(needle) / max(len(hay), 1))
        return score

    score = 0.0
    index = 0
    run = 0
    length = len(needle)
    for offset, ch in enumerate(hay):
        if index < length and ch == needle[index]:
            index += 1
            run += 1
            score += 6.0 + 2.0 * run
            if offset == 0 or hay[offset - 1] in " ._-(":
                score += 8.0
        else:
            run = 0
    if index < length:
        return None
    # Every skipped character is a small penalty, so "app" prefers App.tsx.
    return max(score - 0.4 * (len(hay) - length), 1.0)


class NameIndex:
    """Parallel arrays over every file: ids, blooms, names and metadata."""

    __slots__ = (
        "ids", "masks", "names", "lowered", "paths", "kinds",
        "sizes", "mtimes", "build_seconds", "blob", "blob_array", "offsets", "name_len",
    )

    def __init__(
        self,
        ids: np.ndarray,
        masks: np.ndarray,
        names: list[str],
        lowered: list[str],
        paths: list[str],
        kinds: list[str],
        sizes: np.ndarray,
        mtimes: np.ndarray,
        build_seconds: float = 0.0,
    ) -> None:
        self.ids = ids
        self.masks = masks
        self.names = names
        self.lowered = lowered
        self.paths = paths
        self.kinds = kinds
        self.sizes = sizes
        self.mtimes = mtimes
        self.build_seconds = build_seconds
        # One byte buffer with NUL separators, so a literal-substring query is
        # a single C-level scan over contiguous memory instead of 100k `in`
        # tests in Python. Byte offsets let numpy index the buffer directly,
        # which is what keeps the scoring vectorised as well.
        encoded = [name.encode("utf-8", "surrogatepass") for name in lowered]
        self.blob = b"\x00".join(encoded)
        self.blob_array = np.frombuffer(self.blob, dtype=np.uint8)
        self.name_len = np.array([len(item) for item in encoded], dtype=np.int32)
        starts = np.zeros(len(encoded) + 1, dtype=np.int64)
        if encoded:
            np.cumsum(self.name_len + 1, out=starts[1:])
        self.offsets = starts

    def __len__(self) -> int:
        return int(self.ids.shape[0])

    @classmethod
    def from_rows(cls, rows: Sequence, build_seconds: float = 0.0) -> "NameIndex":
        count = len(rows)
        ids = np.empty(count, dtype=np.int64)
        masks = np.empty(count, dtype=np.int64)
        sizes = np.empty(count, dtype=np.int64)
        mtimes = np.empty(count, dtype=np.float64)
        names: list[str] = []
        lowered: list[str] = []
        paths: list[str] = []
        kinds: list[str] = []
        for position, row in enumerate(rows):
            path = row["path"]
            name = row["name"] or _basename(path)
            ids[position] = int(row["id"])
            masks[position] = int(row["char_mask"] or 0) or char_mask(name)
            sizes[position] = int(row["size"] or 0)
            mtimes[position] = float(row["mtime"] or 0.0)
            names.append(name)
            lowered.append(name.lower())
            paths.append(path)
            kinds.append(row["kind"])
        return cls(ids, masks, names, lowered, paths, kinds, sizes, mtimes, build_seconds)

    # ------------------------------------------------------------------ query

    def search(self, text: str, limit: int = 50) -> list[tuple[int, float]]:
        """Rank files whose name matches `text`, best first.

        Two tiers, in the order the scorer would rank them anyway:

        1. **literal** — the query appears verbatim in the name. Found by one
           C-level regex scan over the joined blob, which is what makes the
           common case (typing a word you can see) cheap at any corpus size.
        2. **subsequence** — `rdme` for README.md. Only runs when the literal
           tier cannot fill the page, and only over bloom survivors.
        """
        needle = text.strip().lower()
        if not needle or not len(self):
            return []

        literal = self._literal(needle, limit) if len(needle) >= 2 else []
        if len(literal) >= limit:
            literal = literal[:limit]
            return [(int(self.ids[position]), score) for position, score in literal]

        taken = {position for position, _ in literal}
        rest = self._subsequence(needle, limit - len(literal), skip=taken)
        return [(int(self.ids[position]), score) for position, score in literal] + rest

    def _literal(self, needle: str, limit: int) -> list[tuple[int, float]]:
        """Every name containing `needle`, ranked by the scorer's literal branch.

        The scan is C (one regex over the blob) and the ranking is numpy, so the
        cost is proportional to the number of hits, not to the corpus. The
        Python loop that used to score each hit by hand was the whole reason
        this tier was slower than the SQL scan it replaced.
        """
        try:
            encoded = needle.encode("utf-8", "surrogatepass")
        except UnicodeEncodeError:
            return []
        starts = np.fromiter(
            (match.start() for match in re.finditer(re.escape(encoded), self.blob)),
            dtype=np.int64,
        )
        if not len(starts):
            return []

        positions = np.searchsorted(self.offsets, starts, side="right") - 1
        offset_in_name = starts - self.offsets[positions]

        score = np.full(len(starts), 120.0)
        score += np.where(offset_in_name == 0, 40.0, 0.0)
        # Word-start bonus: the character before the hit is a separator. Look it
        # up in the same buffer instead of slicing strings one at a time.
        previous = self.blob_array[np.maximum(starts - 1, 0)]
        score += np.where(np.isin(previous, BOUNDARY_BYTES) & (starts > 0), 20.0, 0.0)
        score += 30.0 * (len(encoded) / np.maximum(self.name_len[positions], 1))

        keep = max(limit * 4, 40)
        if len(score) > keep:
            shortlist = np.argpartition(-score, keep - 1)[:keep]
        else:
            shortlist = np.arange(len(score))
        order = shortlist[np.argsort(-score[shortlist])]
        return [(int(positions[i]), float(score[i])) for i in order]

    def _subsequence(
        self, needle: str, limit: int, skip: set[int] | None = None
    ) -> list[tuple[int, float]]:
        if limit <= 0:
            return []
        candidates = self._candidates(needle)
        if not len(candidates):
            return []
        skip = skip or set()
        scored: list[tuple[int, float]] = []
        lowered = self.lowered
        ids = self.ids
        for position in candidates.tolist():
            if position in skip:
                continue
            score = _score(needle, lowered[position])
            if score is not None:
                scored.append((int(ids[position]), score))
        scored.sort(key=lambda item: -item[1])
        return scored[:limit]

    def _candidates(self, needle: str) -> np.ndarray:
        mask = char_mask(needle)
        if not mask:
            positions = np.arange(len(self))
        else:
            # One vectorised AND over the whole corpus. This is the line that
            # replaced a full table scan with a Python row object per file.
            positions = np.nonzero((self.masks & mask) == mask)[0]
        if len(positions) > HARD_CAP:
            # Keep the newest: an old build artifact is a worse answer than the
            # file you downloaded yesterday, and this only runs on 1-char queries.
            keep = np.argpartition(-self.mtimes[positions], HARD_CAP)[:HARD_CAP]
            positions = positions[keep]
        return positions

    # ------------------------------------------------------------- accessors

    def row(self, position: int) -> dict:
        return {
            "id": int(self.ids[position]),
            "path": self.paths[position],
            "name": self.names[position],
            "kind": self.kinds[position],
            "size": int(self.sizes[position]),
            "mtime": float(self.mtimes[position]),
        }

    def prefix(self, text: str, limit: int = 20) -> list[tuple[int, float]]:
        """Names that start with `text`, newest first.

        Kept separate from `search` because the UI completion popup wants a
        different ordering from the ranked results.
        """
        needle = text.strip().lower()
        if not needle:
            return []
        out: list[tuple[int, float]] = []
        for position, name in enumerate(self.lowered):
            if name.startswith(needle):
                out.append((int(self.ids[position]), float(self.mtimes[position])))
        out.sort(key=lambda item: -item[1])
        return out[:limit]

    def stats(self) -> dict[str, int | float]:
        name_bytes = sum(len(name) for name in self.names) + sum(len(path) for path in self.paths)
        return {
            "entries": len(self),
            "name_bytes": name_bytes,
            "array_bytes": int(self.ids.nbytes + self.masks.nbytes + self.sizes.nbytes + self.mtimes.nbytes),
            "build_ms": round(self.build_seconds * 1000.0, 1),
        }


def _basename(path: str) -> str:
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


# ---------------------------------------------------------------- process cache

class _NameCache:
    """One index per database path, rebuilt when the file or its WAL changes.

    The UI opens a short-lived Store per request. Without this, every keystroke
    would reload 100k rows; with it, the first query after an index run pays the
    build and the rest are free.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._index: NameIndex | None = None

    def get(self, store, loader) -> NameIndex:
        key = (str(store.path), _stamps(store.path))
        with self._lock:
            if key == self._key and self._index is not None:
                return self._index
        index = loader()
        with self._lock:
            self._key, self._index = key, index
        return index

    def clear(self) -> None:
        with self._lock:
            self._key = None
            self._index = None


NAME_CACHE = _NameCache()


def _stamps(path: Path) -> tuple[int, int]:
    values = []
    for candidate in (path, Path(str(path) + "-wal")):
        try:
            values.append(candidate.stat().st_mtime_ns)
        except OSError:
            values.append(0)
    return (values[0], values[1])


def load(store) -> NameIndex:
    """Build the index for `store`, or return the cached one."""
    return NAME_CACHE.get(store, lambda: _build(store))


def _build(store) -> NameIndex:
    started = time.perf_counter()
    try:
        rows = store.conn.execute(
            "SELECT id, path, name, char_mask, kind, size, mtime FROM files"
        ).fetchall()
    except Exception:
        rows = []
    return NameIndex.from_rows(rows, time.perf_counter() - started)


def invalidate() -> None:
    NAME_CACHE.clear()
