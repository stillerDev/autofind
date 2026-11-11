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

import numpy as np

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

    __slots__ = ("ids", "doc_len", "avgdl", "vocab", "indptr", "flat_pos", "flat_tfs", "built_in")

    def __init__(self, ids: np.ndarray, doc_len: np.ndarray, avgdl: float,
                 vocab: np.ndarray, indptr: np.ndarray,
                 flat_pos: np.ndarray, flat_tfs: np.ndarray,
                 built_in: float) -> None:
        self.ids = ids          # chunk id per row
        self.doc_len = doc_len  # tokens per chunk, same row order
        self.avgdl = avgdl
        self.vocab = vocab      # sorted term array; row i of the postings
        self.indptr = indptr    # offsets: term i -> flat_pos[indptr[i]:indptr[i+1]]
        self.flat_pos = flat_pos  # int32 row positions, grouped by term
        self.flat_tfs = flat_tfs  # float32 term frequencies, same order
        self.built_in = built_in

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
                    tf_by_term[term] = array("f")
                positions.append(position)
                tf_by_term[term].append(tf)
        id_array = np.asarray(ids, dtype=np.int64)
        doc_len = np.asarray(lengths, dtype=np.float32)
        avgdl = float(doc_len.mean()) if len(doc_len) else 1.0
        # dtype=object, not np.str_: a fixed-width unicode array sizes every
        # term to the *longest* one (a 903-character token in this corpus), so
        # 918 679 terms became 3.3 GB. Object arrays keep pointers to the same
        # strings — ~60 MB — and `searchsorted` compares them the same way.
        vocab = np.array(sorted(pos_by_term), dtype=object)
        indptr = np.zeros(len(vocab) + 1, dtype=np.int64)
        pos_parts: list[np.ndarray] = []
        tfs_parts: list[np.ndarray] = []
        for index, term in enumerate(vocab.tolist()):
            positions = pos_by_term.pop(term)
            tfs = tf_by_term.pop(term)
            indptr[index + 1] = indptr[index] + len(positions)
            # `frombuffer` views the array buffer; `concatenate` copies it into
            # the single flat array, so the per-term buffers can be dropped.
            pos_parts.append(np.frombuffer(positions, dtype=np.int32))
            tfs_parts.append(np.frombuffer(tfs, dtype=np.float32))
        flat_pos = np.concatenate(pos_parts) if pos_parts else np.empty(0, dtype=np.int32)
        flat_tfs = np.concatenate(tfs_parts) if tfs_parts else np.empty(0, dtype=np.float32)
        return cls(id_array, doc_len, avgdl, vocab, indptr, flat_pos, flat_tfs,
                   time.perf_counter() - started)

    def _postings(self, index: int) -> tuple[np.ndarray, np.ndarray]:
        lo, hi = self.indptr[index], self.indptr[index + 1]
        return self.flat_pos[lo:hi], self.flat_tfs[lo:hi]

    def search(self, text: str, limit: int) -> list[tuple[int, float]]:
        """Rank chunks for free text; returns (chunk_id, score) best first.

        Every query token matches its exact term plus every vocabulary term
        that starts with it — the same prefix policy the FTS5 query builder
        applies (`"term"*`), implemented as a sorted-vocab range scan."""
        tokens = tokenize(cjk_bigrams(text))
        if not tokens or not len(self.vocab):
            return []
        count = len(self.ids)
        scores = np.zeros(count, dtype=np.float32)
        matched_any = False
        for token in tokens:
            lo = int(np.searchsorted(self.vocab, token, side="left"))
            matched = 0
            index = lo
            while index < len(self.vocab):
                term = str(self.vocab[index])
                if not term.startswith(token):
                    break
                matched += 1
                if matched <= MAX_PREFIX_TERMS:
                    self._score_index(index, scores)
                    matched_any = True
                index += 1
            if matched == 0 and lo > 0 and str(self.vocab[lo - 1]) == token:
                # str comparison can place the token after its own exact term
                # when locale collation disagrees with numpy's byte order.
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
