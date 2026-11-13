"""In-memory name index: fuzzy file matching without touching SQLite per query.

kensaku keeps every file name in a Rust arena and matches with nucleo, which is
why it answers in ~1.3 ms over 100k files. We used to run one `SELECT` with a
bitwise AND and then build 8 000 Python row objects per query: 35 ms at 8k
files, and it gets worse linearly.

This is the same idea in numpy, taken one step further: the whole fzf-style
subsequence scorer is vectorised. Names, paths and kinds live in flat arrays
(two joined UTF-8 blobs for names and paths, a uint8 code for kinds), the
character bloom prunes the corpus in one AND, and the survivors are matched
greedily — left to right, earliest occurrence first, exactly the strategy the
Python scorer used — with one `searchsorted` per query byte over a precomputed
position table. The scores that fall out of those match positions are the same
numbers `_score` produces, so results are identical, but no Python loop sees
more than the top hits. The worst case stopped being a worst case: a query
that matches nothing over 100k names costs the same couple of milliseconds as
one that fills the page.

Non-ASCII queries keep the Python scorer (byte-level greedy is only
equivalent to character-level greedy for ASCII needles), capped as before.

The index is rebuilt when the database file (or its WAL) changes, so a running
UI server picks up an index run without a restart.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path
from typing import Sequence

from .lazynp import np

from .store import char_mask

# Beyond this many survivors the Python scorer (non-ASCII fallback) sets the
# latency. The survivors are the newest ones, which is the tie-break the
# ranking wants anyway. The vectorised path never caps: its cost is a
# searchsorted per query byte over the survivors, linear and small.
HARD_CAP = 20_000

# Guards against a pathological index (a whole drive at depth 20). Above this we
# fall back to the SQL path instead of eating gigabytes.
MAX_ENTRIES = 1_500_000

# Bytes that make the character after them read as a word start. Stored as
# plain bytes and turned into a numpy array at call time: a module-level
# `np.frombuffer(...)` would import numpy on every cold CLI start.
BOUNDARY_CHARS = b" ._-("

# Byte values the match-position table covers: lowercase ASCII letters and
# digits. A query byte outside it (punctuation, non-ASCII) sends the whole
# query to the Python scorer.
ALPHABET_BYTES = b"abcdefghijklmnopqrstuvwxyz0123456789"
# Bytes the vectorised scorer's position table actually knows. Any other
# ASCII byte (`_`, `-`, `.`, space…) has no indexed positions, so such queries
# take the Python scorer, exactly like non-ASCII ones.
ALPHABET_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


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
    """Parallel arrays over every file: ids, blooms, blobs and metadata.

    Strings are not kept as Python objects. Three joined UTF-8 blobs (lowered
    names, original-case names, full paths — plus a lowered copy of the paths
    for scope queries) with int64 offset tables replace the string lists the
    index used to hold; decoding happens per answer, not per entry. Kinds are
    a uint8 code over a tiny vocabulary. The `names` / `lowered` / `paths` /
    `kinds` attributes are lazy decoded views kept so older call sites keep
    working — touching one materialises it for the lifetime of the index, so
    hot loops use the `name(i)` / `lower(i)` / `path(i)` / `kind(i)` accessors.

    For the vectorised scorer the index also carries, per alphabet byte, the
    sorted positions of that byte in the lowered-name blob. That table is what
    turns a subsequence match into one `searchsorted` per query byte.
    """

    __slots__ = (
        "ids", "masks", "sizes", "mtimes", "build_seconds",
        "blob", "blob_array", "offsets", "name_len", "name_chars",
        "name_blob", "name_blob_array", "name_offsets",
        "path_blob", "path_blob_array", "path_offsets",
        "path_lower_blob", "path_lower_array", "path_lower_offsets",
        "kind_codes", "kind_vocab",
        "alpha_starts", "alpha_positions",
        "_names_view", "_lowered_view", "_paths_view", "_kinds_view",
    )

    def __init__(
        self,
        ids: np.ndarray,
        masks: np.ndarray,
        names: Sequence[str],
        lowered: Sequence[str],
        paths: Sequence[str],
        kinds: Sequence[str],
        sizes: np.ndarray,
        mtimes: np.ndarray,
        build_seconds: float = 0.0,
    ) -> None:
        self.ids = ids
        self.masks = masks
        self.sizes = sizes
        self.mtimes = mtimes
        self.build_seconds = build_seconds
        self._names_view = None
        self._lowered_view = None
        self._paths_view = None
        self._kinds_view = None

        count = len(ids)

        def joined_offsets(lengths: list[int]) -> np.ndarray:
            """Offsets into a NUL-joined blob: offsets[i] is where item i
            starts (every item is followed by one separator), so a slice to
            offsets[i+1] ends on the separator and the accessors strip it.
            This keeps `_literal`'s original offset math untouched."""
            starts = np.zeros(count + 1, dtype=np.int64)
            if count:
                np.cumsum(
                    np.array([length + 1 for length in lengths], dtype=np.int64),
                    out=starts[1:],
                )
            return starts

        # Lowered names: one byte buffer with NUL separators, so a literal-
        # substring query is a single C-level scan over contiguous memory
        # instead of 100k `in` tests in Python.
        encoded_lower = [name.encode("utf-8", "surrogatepass") for name in lowered]
        self.blob = b"\x00".join(encoded_lower)
        self.blob_array = np.frombuffer(self.blob, dtype=np.uint8)
        self.name_len = np.array([len(item) for item in encoded_lower], dtype=np.int32)
        # Char count for the skip penalty: the Python scorer measured len() in
        # characters, which differs from bytes once Cyrillic shows up.
        self.name_chars = np.array([len(item) for item in lowered], dtype=np.int32)
        self.offsets = joined_offsets([len(item) for item in encoded_lower])

        # Original-case names, own blob (lower() can change byte lengths, so
        # the lowered offsets are not reusable).
        encoded_names = [name.encode("utf-8", "surrogatepass") for name in names]
        self.name_blob = b"\x00".join(encoded_names)
        self.name_blob_array = np.frombuffer(self.name_blob, dtype=np.uint8)
        self.name_offsets = joined_offsets([len(item) for item in encoded_names])

        # Full paths, same treatment, plus a lowered copy for scope queries
        # (`in:`): the vectorised prefix check must see exactly what the old
        # per-path `path.lower().startswith()` loop saw. Paths are the largest
        # string in the index — the reason the flat layout matters at 500k+.
        encoded_paths = [path.encode("utf-8", "surrogatepass") for path in paths]
        self.path_blob = b"\x00".join(encoded_paths)
        self.path_blob_array = np.frombuffer(self.path_blob, dtype=np.uint8)
        self.path_offsets = joined_offsets([len(item) for item in encoded_paths])

        encoded_paths_lower = [
            path.lower().encode("utf-8", "surrogatepass") for path in paths
        ]
        self.path_lower_blob = b"\x00".join(encoded_paths_lower)
        self.path_lower_array = np.frombuffer(self.path_lower_blob, dtype=np.uint8)
        self.path_lower_offsets = joined_offsets(
            [len(item) for item in encoded_paths_lower]
        )

        # Kinds repeat to a handful of values: a uint8 code over a tiny
        # vocabulary costs 100k bytes where a string list cost pointers.
        self.kind_vocab: list[str] = []
        self.kind_codes = np.zeros(count, dtype=np.uint8)
        seen: dict[str, int] = {}
        for position, kind in enumerate(kinds):
            code = seen.get(kind)
            if code is None:
                code = len(self.kind_vocab)
                seen[kind] = code
                self.kind_vocab.append(kind)
            self.kind_codes[position] = code

        # Sorted positions of every alphabet byte inside the lowered-name
        # blob — the backbone of the vectorised subsequence match. One
        # C-level scan per byte value at build, ~4 ms at 100k names; the
        # table is roughly the size of the blob itself, int32.
        chunks: list[np.ndarray] = []
        alpha_starts = np.zeros(len(ALPHABET_BYTES) + 1, dtype=np.int64)
        for column, byte in enumerate(ALPHABET_BYTES):
            hits = np.flatnonzero(self.blob_array == byte)
            chunks.append(hits.astype(np.int32, copy=False))
            alpha_starts[column + 1] = alpha_starts[column] + len(hits)
        self.alpha_starts = alpha_starts
        self.alpha_positions = (
            np.concatenate(chunks).astype(np.int32, copy=False)
            if chunks else np.empty(0, dtype=np.int32)
        )

    def __len__(self) -> int:
        return int(self.ids.shape[0])

    # ------------------------------------------------------- disk persistence

    SIDE_SUFFIX = ".nameidx"

    @staticmethod
    def _bin_suffix(stamps) -> str:
        """A hashable name for the companion file, identical on both sides.

        repr() cannot do this: the savers' tuple holds ints and the loader's
        comes back from a float64 npz (100000 vs 100000.0), which hashed to a
        different file name and silently forced a full RAM rebuild."""
        parts = []
        for index, value in enumerate(stamps):
            parts.append(str(int(value)) if index < 2 else f"{float(value):.6f}")
        return hashlib.sha1("|".join(parts).encode()).hexdigest()[:10]

    def save_sidecar(self, path, stamps: tuple[int, int]) -> None:
        """Persist every build product to `<db>.nameidx` so the next process
        can `np.load` it instead of re-encoding 100k rows (~450 ms). Best
        effort by design: a failed save just costs the next process a rebuild.

        Compressed: the payload is mostly path and name text, so `savez`
        shrank 8.2 MB to 1.6 MB on the benchmark corpus for ~200 ms of
        one-time CPU at save and ~10 ms extra per load. The cold CLI never
        loads it (it uses the SQL scan), so only a server's first fuzzy query
        pays the load.
        """
        # The mmapped companion file `.nameidx.<fingerprint>.bin` carries the
        # four text blobs AND every numeric array raw: both stay file-backed
        # instead of becoming ~40 MB of private RAM. The fingerprint is part
        # of the name on purpose: a server holding a mapping of the previous
        # file must never collide with the rewrite of a reindexed one (Windows
        # refuses or truncates a write over a live mapping), so a rebuild
        # writes a new file and the loader reads the one its npz names.
        NUMERIC_ORDER = (
            ("ids", self.ids), ("masks", self.masks), ("sizes", self.sizes),
            ("mtimes", self.mtimes), ("name_len", self.name_len),
            ("name_chars", self.name_chars), ("offsets", self.offsets),
            ("name_offsets", self.name_offsets), ("path_offsets", self.path_offsets),
            ("path_lower_offsets", self.path_lower_offsets),
            ("kind_codes", self.kind_codes), ("alpha_starts", self.alpha_starts),
            ("alpha_positions", self.alpha_positions),
        )
        blob_sizes = np.array(
            [len(self.blob_array), len(self.name_blob_array),
             len(self.path_blob_array), len(self.path_lower_array)],
            dtype=np.int64,
        )
        numeric_sizes = np.array([arr.nbytes for _, arr in NUMERIC_ORDER], dtype=np.int64)
        payload = {
            "kind_vocab": np.array(self.kind_vocab, dtype=np.str_),
            "bin_sizes": blob_sizes,
            "numeric_sizes": numeric_sizes,
            "numeric_order": np.array([name for name, _ in NUMERIC_ORDER], dtype=np.str_),
            "stamps": np.array(stamps, dtype=np.float64),
            "build_seconds": np.array([self.build_seconds], dtype=np.float64),
        }
        # Written straight to the final path: on Windows a just-closed file can
        # stay locked for a while by antivirus scans, which made the classic
        # tmp-then-replace dance fail repeatedly. A torn write here is safe —
        # the loader validates the files and falls back to a rebuild.
        suffix = self._bin_suffix(stamps)
        bin_path = Path(str(path) + "." + suffix + ".bin")
        with open(bin_path, "wb") as fh:
            fh.write(self.blob_array.tobytes())
            fh.write(self.name_blob_array.tobytes())
            fh.write(self.path_blob_array.tobytes())
            fh.write(self.path_lower_array.tobytes())
            for _, arr in NUMERIC_ORDER:
                fh.write(np.ascontiguousarray(arr).tobytes())
        with open(path, "wb") as fh:
            np.savez_compressed(fh, **payload)

    @classmethod
    def from_arrays(cls, arrays: dict, build_seconds: float = 0.0,
                    blobs: tuple | None = None) -> "NameIndex":
        """Rebuild from a sidecar's payloads. `blobs`, when given, are the four
        uint8 views of the mmapped `.nameidx.bin` companion file: the text
        stays file-backed instead of becoming ~25 MB of private RAM. Without
        them the blobs are materialised from the payload arrays (the legacy
        npz layout, which shipped them compressed)."""
        index = cls.__new__(cls)
        index.ids = arrays["ids"]
        index.masks = arrays["masks"]
        index.sizes = arrays["sizes"]
        index.mtimes = arrays["mtimes"]
        index.build_seconds = build_seconds
        index._names_view = None
        index._lowered_view = None
        index._paths_view = None
        index._kinds_view = None
        if blobs is not None:
            index.blob, index.name_blob, index.path_blob, index.path_lower_blob = blobs
            index.blob_array = index.blob
            index.name_blob_array = index.name_blob
            index.path_blob_array = index.path_blob
            index.path_lower_array = index.path_lower_blob
        else:
            index.blob = np.asarray(arrays["blob"], dtype=np.uint8)
            index.blob_array = index.blob
            index.name_blob = np.asarray(arrays["name_blob"], dtype=np.uint8)
            index.name_blob_array = index.name_blob
            index.path_blob = np.asarray(arrays["path_blob"], dtype=np.uint8)
            index.path_blob_array = index.path_blob
            index.path_lower_blob = np.asarray(arrays["path_lower_blob"], dtype=np.uint8)
            index.path_lower_array = index.path_lower_blob
        index.name_len = arrays["name_len"]
        index.name_chars = arrays["name_chars"]
        index.offsets = arrays["offsets"]
        index.name_offsets = arrays["name_offsets"]
        index.path_offsets = arrays["path_offsets"]
        index.path_lower_offsets = arrays["path_lower_offsets"]
        index.kind_vocab = [str(k) for k in arrays["kind_vocab"].tolist()]
        index.kind_codes = arrays["kind_codes"]
        index.alpha_starts = arrays["alpha_starts"]
        index.alpha_positions = arrays["alpha_positions"]
        return index

    # -------------------------------------------------------------- accessors

    def _slice(self, buf, start: int, end: int) -> bytes:
        """One record out of a blob. The blobs are uint8 arrays (mmapped when
        they came from the sidecar companion), so the slice is copied into
        bytes first — a record is tens of bytes, never the blob."""
        return bytes(buf[start:end]).rstrip(b"\x00")

    def name(self, position: int) -> str:
        return self._slice(
            self.name_blob, self.name_offsets[position], self.name_offsets[position + 1]
        ).decode("utf-8", "surrogatepass")

    def lower(self, position: int) -> str:
        return self._slice(
            self.blob, self.offsets[position], self.offsets[position + 1]
        ).decode("utf-8", "surrogatepass")

    def path(self, position: int) -> str:
        return self._slice(
            self.path_blob, self.path_offsets[position], self.path_offsets[position + 1]
        ).decode("utf-8", "surrogatepass")

    def kind(self, position: int) -> str:
        return self.kind_vocab[self.kind_codes[position]]

    # Lazy decoded views: compatibility for call sites that iterate a column.
    @property
    def names(self) -> list[str]:
        if self._names_view is None:
            self._names_view = [self.name(i) for i in range(len(self))]
        return self._names_view

    @property
    def lowered(self) -> list[str]:
        if self._lowered_view is None:
            self._lowered_view = [self.lower(i) for i in range(len(self))]
        return self._lowered_view

    @property
    def paths(self) -> list[str]:
        if self._paths_view is None:
            self._paths_view = [self.path(i) for i in range(len(self))]
        return self._paths_view

    @property
    def kinds(self) -> list[str]:
        if self._kinds_view is None:
            self._kinds_view = [
                self.kind_vocab[code] for code in self.kind_codes.tolist()
            ]
        return self._kinds_view

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
           tier cannot fill the page, and only over bloom survivors, scored
           vectorised for ASCII queries.
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
            (match.start() for match in re.finditer(re.escape(encoded), memoryview(self.blob))),
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
        score += np.where(
            np.isin(previous, np.frombuffer(BOUNDARY_CHARS, dtype=np.uint8))
            & (starts > 0),
            20.0,
            0.0,
        )
        # The Python scorer measured both the query and the name in
        # characters, not bytes — the two differ the moment Cyrillic shows
        # up, and the tiers must agree with it.
        score += 30.0 * (len(needle) / np.maximum(self.name_chars[positions], 1))

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
        if skip:
            keep = np.fromiter(
                (position not in skip for position in candidates.tolist()),
                dtype=bool,
                count=len(candidates),
            )
            candidates = candidates[keep]
            if not len(candidates):
                return []

        if needle.isascii() and ALPHABET_CHARS.issuperset(needle):
            positions, scores = self._vectorised_scores(needle, candidates)
            if not len(positions):
                return []
            order = np.argsort(-scores, kind="stable")[:limit]
            return [
                (int(self.ids[positions[i]]), float(scores[i])) for i in order
            ]

        # Byte-level greedy is only equivalent to character-level greedy for
        # ASCII needles, so anything else goes back to the Python scorer, over
        # the capped candidate set.
        lowered = self.lowered
        ids = self.ids
        scored: list[tuple[int, float]] = []
        for position in candidates.tolist():
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
        falls_back = not needle.isascii() or not ALPHABET_CHARS.issuperset(needle)
        if len(positions) > HARD_CAP and falls_back:
            # Keep the newest: an old build artifact is a worse answer than the
            # file you downloaded yesterday. Only the Python fallback needs the
            # cap — the vectorised path stays exact and uncapped.
            keep = np.argpartition(-self.mtimes[positions], HARD_CAP)[:HARD_CAP]
            positions = positions[keep]
        return positions

    def _vectorised_scores(
        self, needle: str, candidates: np.ndarray
    ) -> tuple[np.ndarray, np.ndarray]:
        """Greedy subsequence match + fzf score, all in numpy, all exact.

        The Python scorer walks the name once, taking the earliest occurrence
        of each successive query character — and taking the earliest occurrence
        is the optimal strategy for subsequence existence, so the greedy walk
        both decides the match and yields the very positions the score is
        computed from. The same walk vectorises over candidates: for query
        byte b, the next match of every candidate is one `searchsorted` into
        the sorted table of b's positions in the blob, and a found position
        belongs to the candidate whenever it is still inside the candidate's
        byte range. Run bonuses, word-start bonuses and the skip penalty read
        off those positions; the numbers are identical to `_score`'s.
        """
        encoded = needle.encode("ascii", "surrogatepass")
        starts = self.offsets[candidates].astype(np.int32)
        ends = starts + self.name_len[candidates]
        cursor = starts.copy()
        alive = np.ones(len(candidates), dtype=bool)

        match_positions = np.empty((len(encoded), len(candidates)), dtype=np.int32)
        for step, byte in enumerate(encoded):
            column = int(np.nonzero(np.frombuffer(ALPHABET_BYTES, dtype=np.uint8) == byte)[0][0])
            table = self.alpha_positions[
                self.alpha_starts[column]:self.alpha_starts[column + 1]
            ]
            if not len(table):
                alive[:] = False
                break
            index = np.searchsorted(table, cursor, side="left")
            over = index >= len(table)
            found = table[np.minimum(index, len(table) - 1)]
            ok = alive & ~over & (found < ends)
            match_positions[step] = np.where(ok, found, -1)
            cursor = np.where(ok, found + 1, cursor).astype(np.int32)
            alive = ok
            if not alive.any():
                break

        survivors = alive.nonzero()[0]
        if not len(survivors):
            return candidates[:0], np.empty(0, dtype=np.float64)

        picked = match_positions[:, survivors]
        if len(encoded) == 1:
            # A one-byte query never reaches the literal tier (`_literal`
            # starts at two bytes), but the Python scorer still gave its
            # literal branch to every name containing the character — and for
            # one byte, "contains" and "subsequence" are the same set. So the
            # literal formula is the score here, skip penalty included-not.
            position = picked[0]
            offset_in_name = position - starts[survivors]
            score = np.full(len(survivors), 120.0)
            score += np.where(offset_in_name == 0, 40.0, 0.0)
            previous = self.blob_array[np.maximum(position.astype(np.int64) - 1, 0)]
            score += np.where(
                (offset_in_name != 0)
                & np.isin(previous, np.frombuffer(BOUNDARY_CHARS, dtype=np.uint8)),
                20.0,
                0.0,
            )
            score += 30.0 / np.maximum(
                self.name_chars[candidates[survivors]], 1
            ).astype(np.float64)
            return candidates[survivors], score

        score = np.zeros(len(survivors), dtype=np.float64)
        run = np.ones(len(survivors), dtype=np.int64)
        for step in range(len(encoded)):
            position = picked[step]
            score += 6.0 + 2.0 * run
            at_start = position == starts[survivors]
            previous = self.blob_array[np.maximum(position.astype(np.int64) - 1, 0)]
            score += np.where(
                at_start
                | np.isin(previous, np.frombuffer(BOUNDARY_CHARS, dtype=np.uint8)),
                8.0,
                0.0,
            )
            if step + 1 < len(encoded):
                run = np.where(picked[step + 1] == position + 1, run + 1, 1)
        # Every skipped character is a small penalty, so "app" prefers App.tsx.
        score -= 0.4 * (self.name_chars[candidates[survivors]] - len(encoded))
        score = np.maximum(score, 1.0)
        return candidates[survivors], score

    # ------------------------------------------------------------ path scopes

    def scope_positions(self, prefixes: Sequence[str]) -> np.ndarray:
        """Positions whose path is `prefix` itself or inside it.

        The vectorised replacement for the per-path `startswith` loop that
        `_scope_ids` used to run: one byte comparison per prefix byte over the
        lowered path blob, plus a directory-boundary check, all in C.
        """
        count = len(self)
        if not prefixes or not count:
            return np.empty(0, dtype=np.int64)
        mask = np.zeros(count, dtype=bool)
        separators = np.frombuffer(b"/\\", dtype=np.uint8)
        lengths = self.path_lower_offsets[1:] - self.path_lower_offsets[:-1] - 1
        for prefix in prefixes:
            encoded = prefix.encode("utf-8", "surrogatepass")
            if not encoded:
                mask[:] = True
                continue
            length = len(encoded)
            ok = lengths >= length
            for offset, byte in enumerate(encoded):
                ok &= (
                    self.path_lower_array[self.path_lower_offsets[:-1] + offset]
                    == byte
                )
            if not ok.any():
                continue
            has_next = lengths > length
            next_byte = np.zeros(count, dtype=np.uint8)
            if has_next.any():
                index = self.path_lower_offsets[:-1] + length
                next_byte[has_next] = self.path_lower_array[index[has_next]]
            ok &= (lengths == length) | np.isin(next_byte, separators)
            mask |= ok
        return np.nonzero(mask)[0]

    # ------------------------------------------------------------- accessors

    def row(self, position: int) -> dict:
        return {
            "id": int(self.ids[position]),
            "path": self.path(position),
            "name": self.name(position),
            "kind": self.kind(position),
            "size": int(self.sizes[position]),
            "mtime": float(self.mtimes[position]),
        }

    def prefix(self, text: str, limit: int = 20) -> list[tuple[int, float]]:
        """Names that start with `text`, newest first.

        Vectorised over the lowered blob: one byte comparison per query byte,
        no Python loop over names. Kept separate from `search` because the UI
        completion popup wants a different ordering from the ranked results.
        """
        needle = text.strip().lower()
        if not needle:
            return []
        try:
            encoded = needle.encode("utf-8", "surrogatepass")
        except UnicodeEncodeError:
            return []
        ok = self.name_len >= len(encoded)
        for offset, byte in enumerate(encoded):
            ok &= self.blob_array[self.offsets[:-1] + offset] == byte
            if not ok.any():
                return []
        positions = np.nonzero(ok)[0]
        order = positions[np.argsort(-self.mtimes[positions], kind="stable")][:limit]
        return [(int(self.ids[i]), float(self.mtimes[i])) for i in order]

    def stats(self) -> dict[str, int | float]:
        return {
            "entries": len(self),
            "name_bytes": int(len(self.name_blob) + len(self.path_blob)),
            "array_bytes": int(
                self.ids.nbytes + self.masks.nbytes + self.sizes.nbytes
                + self.mtimes.nbytes + len(self.blob) + len(self.path_lower_blob)
                + self.alpha_positions.nbytes
            ),
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


def search_rows(store, text: str, limit: int = 50) -> list[tuple[int, float]]:
    """A numpy-free name scan for one-shot CLI processes.

    Importing numpy costs ~70 ms on Windows, which is most of a cold
    `autofind search`; the numpy index costs another 70-150 ms to build. The
    files table already carries a denormalised lowercased name and the same
    64-bit character bloom the vectorised path prefilter uses, so a Python pass
    over them answers the query in a few milliseconds. Only chosen before
    numpy is loaded: the app server keeps the fast in-memory index.
    """
    needle = text.strip().lower()
    if not needle or limit <= 0:
        return []
    mask = char_mask(needle)
    try:
        rows = store.conn.execute(
            "SELECT id, name, char_mask FROM files"
        ).fetchall()
    except Exception:
        return []
    scored: list[tuple[float, int]] = []
    for row in rows:
        if mask and (int(row[2] or 0) & mask) != mask:
            continue
        score = _score(needle, str(row[1] or "").lower())
        if score is not None:
            scored.append((score, int(row[0])))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [(file_id, score) for score, file_id in scored[:limit]]


def _fingerprint(store) -> tuple[int, int, float, float]:
    """Content stamp of the files table. File mtimes are not usable here: even
    a read-only WAL open can touch the -wal file's clock, which would make the
    sidecar look stale after every process start. Row facts cannot lie."""
    row = store.conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(MAX(mtime), 0.0), "
        "COALESCE(MAX(indexed_at), 0.0) FROM files"
    ).fetchone()
    return (int(row[0]), int(row[1]), float(row[2]), float(row[3]))


def _build(store) -> NameIndex:
    fingerprint = _fingerprint(store)
    index = _load_sidecar(store, fingerprint)
    if index is not None:
        return index
    started = time.perf_counter()
    try:
        rows = store.conn.execute(
            "SELECT id, path, name, char_mask, kind, size, mtime FROM files"
        ).fetchall()
    except Exception:
        rows = []
    index = NameIndex.from_rows(rows, time.perf_counter() - started)
    from . import vault as _vault_mod

    if _vault_mod.current() is not None:
        # Paranoid mode: the sidecar's blobs are file names and paths in
        # plaintext, which is exactly what seal promises not to store. The
        # index is rebuilt in RAM on each server start (~0.5 s at 100k).
        return index
    try:
        index.save_sidecar(Path(str(store.path) + NameIndex.SIDE_SUFFIX), fingerprint)
    except Exception:
        pass  # the sidecar is a cache, never a requirement
    return index


def _load_sidecar(store, fingerprint: tuple[int, int, float, float]) -> NameIndex | None:
    """Restore the persisted index when the files table has not moved. ~456 ms
    of row decoding becomes one ~40 MB sequential read, and the four text
    blobs are mmapped from the companion file instead of becoming ~25 MB of
    private RAM."""
    path = Path(str(store.path) + NameIndex.SIDE_SUFFIX)
    try:
        if not path.exists():
            return None
        with np.load(path, allow_pickle=False) as data:
            saved = tuple(data["stamps"].tolist())
            if len(saved) == 2:
                saved = (saved[0], saved[1], 0.0, 0.0)  # sidecar predates the fingerprint switch
            elif len(saved) != 4:
                return None
            if saved != fingerprint:
                return None
            blobs = None
            arrays = {}
            if "bin_sizes" in data.files:
                sizes = data["bin_sizes"].tolist()
                suffix = NameIndex._bin_suffix(saved)
                bin_path = Path(str(path) + "." + suffix + ".bin")
                # The companion carries the four text blobs AND, in the newer
                # layout, every numeric array raw — the expected size grows
                # with the payload, so gate on both, not on the blobs alone.
                expected = sum(sizes)
                numeric_sizes = None
                numeric_order = None
                if "numeric_sizes" in data.files and "numeric_order" in data.files:
                    numeric_sizes = data["numeric_sizes"].tolist()
                    numeric_order = [str(n) for n in data["numeric_order"].tolist()]
                    expected += sum(numeric_sizes)
                if bin_path.exists() and bin_path.stat().st_size == expected:
                    view = np.memmap(bin_path, dtype=np.uint8, mode="r")
                    edges = np.concatenate(([0], np.cumsum(sizes)))
                    blobs = tuple(view[edges[i]:edges[i + 1]] for i in range(4))
                    if numeric_sizes is not None:
                        dtypes = {"ids": np.int64, "masks": np.int64, "sizes": np.int64,
                                  "mtimes": np.float64, "name_len": np.int32,
                                  "name_chars": np.int32, "offsets": np.int64,
                                  "name_offsets": np.int64, "path_offsets": np.int64,
                                  "path_lower_offsets": np.int64, "kind_codes": np.uint8,
                                  "alpha_starts": np.int64, "alpha_positions": np.int32}
                        offset = int(sum(sizes))
                        for name, size in zip(numeric_order, numeric_sizes):
                            arrays[name] = np.frombuffer(
                                view[offset:offset + size], dtype=dtypes[name]
                            )
                            offset += size
            for name in ("ids", "masks", "sizes", "mtimes", "name_len", "name_chars",
                         "offsets", "name_offsets", "path_offsets", "path_lower_offsets",
                         "kind_codes", "alpha_starts", "alpha_positions",
                         "kind_vocab", "blob", "name_blob", "path_blob", "path_lower_blob"):
                if name not in arrays and name in data.files:
                    arrays[name] = data[name]
            index = NameIndex.from_arrays(
                arrays, build_seconds=float(data["build_seconds"][0]), blobs=blobs
            )
        if len(index.ids) == 0 and _row_count(store) > 0:
            return None
        return index
    except Exception:
        try:
            path.unlink()  # a torn or stale sidecar must not linger
            for orphan in path.parent.glob(path.name + ".*.bin"):
                orphan.unlink()
        except OSError:
            pass
        return None


def _row_count(store) -> int:
    try:
        return int(store.conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
    except Exception:
        return 0


def invalidate() -> None:
    NAME_CACHE.clear()
