"""In-RAM BM25 over the chunk table — the role tantivy plays for Kensaku.

FTS5 answers a non-selective OR query by scoring every matching row inside
SQLite; on a corpus where most chunks share their vocabulary that costs
~100 ms per query. This module keeps an inverted index as flat numpy arrays
(CSR-style: one postings array plus per-term offsets), so the same query is a
few numpy gathers and a matvec — single-digit milliseconds at 100k chunks
while the whole index costs ~25 MB, not the ~300 MB a dict of per-term arrays
would spend on Python object overhead.

Built lazily in a background thread on the first lexical query; until it is
ready the caller falls back to the FTS5 path, so neither the cold CLI nor the
first server request pay the build. Tokenization here is its own consistent
scheme (case-folded `\\w` runs minus underscore, no diacritic folding), never
mixed with FTS5's index — a query tokenized here only ever matches postings
tokenized here.
"""

from __future__ import annotations

import math
import re
import time
from array import array
from collections.abc import Iterable

from .lazynp import np

from .cjk import cjk_bigrams

# unicode61 splits on everything that is not alphanumeric; \w adds underscore,
# so subtract it. Both sides of the index use this one regex.
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
K1 = 1.2
B = 0.75
MAX_PREFIX_TERMS = 200  # safety cap on a prefix expansion


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text)]


class InRamBM25:
    """CSR-style postings over every chunk. Immutable once built; the store
    keeps one instance and rebuilds it when the chunk table changes."""

    __slots__ = ("ids", "doc_len", "avgdl", "vocab_blob", "vocab_offsets", "indptr", "flat_pos", "flat_tfs", "built_in", "fingerprint")

    def __init__(self, ids: np.ndarray, doc_len: np.ndarray, avgdl: float,
                 vocab_blob: bytes, vocab_offsets: np.ndarray, indptr: np.ndarray,
                 flat_pos: np.ndarray, flat_tfs: np.ndarray,
                 built_in: float) -> None:
        self.ids = ids          # chunk id per row
        self.doc_len = doc_len  # tokens per chunk, same row order
        self.avgdl = avgdl
        self.vocab_blob = vocab_blob  # sorted terms, UTF-8, back to back
        self.vocab_offsets = vocab_offsets  # term i -> blob[vocab_offsets[i]:+1]
        self.indptr = indptr    # offsets: term i -> flat_pos[indptr[i]:indptr[i+1]]
        self.flat_pos = flat_pos  # int32 row positions, grouped by term
        self.flat_tfs = flat_tfs  # float32 term frequencies, same order
        self.built_in = built_in
        self.fingerprint = None  # (chunks count, max id) when a sealed sidecar carries it

    @classmethod
    def build(cls, rows: Iterable[tuple[int, str]]) -> "InRamBM25":
        started = time.perf_counter()
        # `rows` may be a generator over the chunk table: consuming it lazily
        # keeps one document's text alive at a time instead of all 322 MB of
        # them, which is ~470 MB of peak RSS on the 298k-chunk index.
        ids: list[int] = []
        lengths: list[int] = []
        # One posting is (position, tf). A list of Python tuples costs ~64 bytes
        # per posting — 1.2 GB at 19M postings — so the postings accumulate in
        # `array` buffers (4 bytes each, 4x the final size) and are copied into
        # the flat arrays once. Same time, a quarter of the memory.
        pos_by_term: dict[str, array] = {}
        tf_by_term: dict[str, array] = {}
        for position, (chunk_id, text) in enumerate(rows):
            ids.append(chunk_id)
            # The same bigram transform the FTS index applies, so a CJK query
            # that works through SQLite also works through this index instead
            # of being one opaque token per run.
            tokens = tokenize(cjk_bigrams(text))
            lengths.append(len(tokens))
            counts: dict[str, int] = {}
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            for term, tf in counts.items():
                positions = pos_by_term.get(term)
                if positions is None:
                    positions = pos_by_term[term] = array("i")
                    tf_by_term[term] = array("H")
                positions.append(position)
                tf_by_term[term].append(tf)
        id_array = np.asarray(ids, dtype=np.int64)
        doc_len = np.asarray(lengths, dtype=np.float32)
        avgdl = float(doc_len.mean()) if len(doc_len) else 1.0
        # The vocabulary is one UTF-8 blob plus an offset table, not an array
        # of Python strings: an object array costs ~60 MB of string objects at
        # 900k terms, the blob ~18 MB, and UTF-8 byte order IS codepoint order,
        # so bisect over the blob ranks terms exactly like sorted() did.
        sorted_terms = sorted(pos_by_term)
        vocab_parts = []
        vocab_offsets = np.zeros(len(sorted_terms) + 1, dtype=np.int64)
        for index, term in enumerate(sorted_terms):
            encoded = term.encode("utf-8")
            vocab_parts.append(encoded)
            vocab_offsets[index + 1] = vocab_offsets[index] + len(encoded)
        vocab_blob = b"".join(vocab_parts)
        indptr = np.zeros(len(sorted_terms) + 1, dtype=np.int64)
        pos_parts: list[np.ndarray] = []
        tfs_parts: list[np.ndarray] = []
        for index, term in enumerate(sorted_terms):
            positions = pos_by_term.pop(term)
            tfs = tf_by_term.pop(term)
            indptr[index + 1] = indptr[index] + len(positions)
            # `frombuffer` views the array buffer; `concatenate` copies it into
            # the single flat array, so the per-term buffers can be dropped.
            pos_parts.append(np.frombuffer(positions, dtype=np.int32))
            tfs_parts.append(np.frombuffer(tfs, dtype=np.uint16))
        flat_pos = np.concatenate(pos_parts) if pos_parts else np.empty(0, dtype=np.int32)
        flat_tfs = np.concatenate(tfs_parts) if tfs_parts else np.empty(0, dtype=np.uint16)
        return cls(id_array, doc_len, avgdl, vocab_blob, vocab_offsets, indptr, flat_pos, flat_tfs,
                   time.perf_counter() - started)

    def to_arrays(self) -> dict:
        """npz-ready dict of the CSR - the sealed sidecar stores exactly this."""
        return {
            "ids": self.ids, "doc_len": self.doc_len,
            "avgdl": np.asarray([self.avgdl], dtype=np.float64),
            "vocab_blob": np.frombuffer(self.vocab_blob, dtype=np.uint8),
            "vocab_offsets": self.vocab_offsets, "indptr": self.indptr,
            "flat_pos": self.flat_pos, "flat_tfs": self.flat_tfs,
        }

    def write_memmap_sidecar(self, prefix) -> None:
        """Persist the two big arrays as .npy files so the next process can
        mmap them instead of holding ~15 MB of private RAM. The fingerprint
        is part of the file name: a rebuild writes new files rather than
        touching ones another process may be reading (Windows)."""
        import numpy as np
        prefix = str(prefix)
        np.save(prefix + ".pos.npy", np.ascontiguousarray(self.flat_pos))
        np.save(prefix + ".tfs.npy", np.ascontiguousarray(self.flat_tfs))

    def attach_memmap(self, prefix) -> None:
        """Swap the two big in-RAM arrays for file-backed views. The pages
        fault in on demand and live in the OS file cache - they count toward
        RSS but never toward private (USS) memory."""
        import numpy as np
        prefix = str(prefix)
        self.flat_pos = np.load(prefix + ".pos.npy", mmap_mode="r")
        self.flat_tfs = np.load(prefix + ".tfs.npy", mmap_mode="r")

    @classmethod
    def from_arrays(cls, arrays: dict) -> "InRamBM25":
        index = cls.__new__(cls)
        index.ids = arrays["ids"]
        index.doc_len = arrays["doc_len"]
        avg = arrays.get("avgdl")
        index.avgdl = float(avg[0]) if avg is not None and avg.size else 1.0
        index.vocab_blob = bytes(arrays["vocab_blob"])
        index.vocab_offsets = arrays["vocab_offsets"]
        index.indptr = arrays["indptr"]
        index.flat_pos = arrays["flat_pos"]
        index.flat_tfs = arrays["flat_tfs"]
        index.built_in = 0.0
        fp = arrays.get("fingerprint")
        index.fingerprint = tuple(int(v) for v in fp) if fp is not None else None
        return index

    def _term(self, index: int) -> str:
        lo, hi = self.vocab_offsets[index], self.vocab_offsets[index + 1]
        return self.vocab_blob[lo:hi].decode("utf-8")

    def _postings(self, index: int) -> tuple[np.ndarray, np.ndarray]:
        lo, hi = self.indptr[index], self.indptr[index + 1]
        return self.flat_pos[lo:hi], self.flat_tfs[lo:hi]

    def search(self, text: str, limit: int) -> list[tuple[int, float]]:
        """Rank chunks for free text; returns (chunk_id, score) best first.

        Every query token matches its exact term plus every vocabulary term
        that starts with it — the same prefix policy the FTS5 query builder
        applies (`"term"*`), implemented as a sorted-vocab range scan."""
        tokens = tokenize(cjk_bigrams(text))
        if not tokens or not len(self.vocab_offsets) > 1:
            return []
        count = len(self.ids)
        scores = np.zeros(count, dtype=np.float32)
        matched_any = False
        n_terms = len(self.vocab_offsets) - 1
        for token in tokens:
            # Binary search over the UTF-8 blob: byte order is codepoint order,
            # so this ranks terms exactly like sorted() did.
            needle = token.encode("utf-8")
            lo, hi = 0, n_terms
            while lo < hi:
                mid = (lo + hi) // 2
                term = self._term(mid)
                if term < token:
                    lo = mid + 1
                else:
                    hi = mid
            matched = 0
            index = lo
            while index < n_terms:
                term = self._term(index)
                if not term.startswith(token):
                    break
                matched += 1
                if matched <= MAX_PREFIX_TERMS:
                    self._score_index(index, scores)
                    matched_any = True
                index += 1
            if matched == 0 and lo > 0 and self._term(lo - 1) == token:
                # an exact term can sit just before the insertion point when
                # the prefix scan started past it
                self._score_index(lo - 1, scores)
                matched_any = True
        if not matched_any:
            return []
        hits = np.flatnonzero(scores)
        if not len(hits):
            return []
        take = min(limit, len(hits))
        top = hits[np.argpartition(-scores[hits], take - 1)[:take]]
        top = top[np.argsort(-scores[top])]
        return [(int(self.ids[i]), float(scores[i])) for i in top]

    def _score_index(self, index: int, scores: np.ndarray) -> None:
        positions, tfs = self._postings(index)
        df = len(positions)
        if not df:
            return
        idf = math.log((len(self.ids) - df + 0.5) / (df + 0.5) + 1.0)
        dl = self.doc_len[positions]
        scores[positions] += idf * (tfs * (K1 + 1.0)) / (
            tfs + K1 * (1.0 - B + B * dl / self.avgdl)
        )
