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

import io
import math
import mmap
import re
import struct
import time
from array import array

try:
    import zstandard as _zstd
except ImportError:  # optional: uncompressed AFS2 stays valid
    _zstd = None
from collections import OrderedDict
from collections.abc import Iterable
import threading

from .lazynp import np

from .cjk import cjk_bigrams

# unicode61 splits on everything that is not alphanumeric; \w adds underscore,
# so subtract it. Both sides of the index use this one regex.
TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
K1 = 1.2
B = 0.75
MAX_PREFIX_TERMS = 200  # safety cap on a prefix expansion

# Segmented sealed sidecar (AFS2): see SealedSegmentBM25. One decrypt per
# ~SEG_POSTINGS instead of one decrypt of the whole index.
SEG_MAGIC = b"AFS2SEGS"
SEG_POSTINGS = 1_000_000
# Decrypted segments live in one bounded LRU; the cap keeps a pathological
# query (a two-letter prefix over 200 vocabulary terms) from pulling the
# whole index into private RAM.
SEG_CACHE_BYTES = 16 * 1024 * 1024


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text)]


def _narrow(arr: "np.ndarray") -> "np.ndarray":
    """int64 -> int32 when every value fits: halves the CSR's private RAM.

    313k chunk ids, ~900k vocab offsets and ~900k indptr entries spend
    ~11 MB as int64 on this index; int32 is enough for any realistic
    corpus (a 2-billion-row SQLite index would outgrow RAM anyway) and
    every consumer here only slices or compares values."""
    if arr is None or arr.dtype != np.int64:
        return arr
    if arr.size == 0:
        return arr.astype(np.int32)
    if int(arr.max()) < 2**31 - 1 and int(arr.min()) > -(2**31):
        return arr.astype(np.int32)
    return arr


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
        return cls(_narrow(id_array), doc_len, avgdl, vocab_blob, _narrow(vocab_offsets), _narrow(indptr), flat_pos, flat_tfs,
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
        index.ids = _narrow(arrays["ids"])
        index.doc_len = arrays["doc_len"]
        avg = arrays.get("avgdl")
        index.avgdl = float(avg[0]) if avg is not None and avg.size else 1.0
        index.vocab_blob = bytes(arrays["vocab_blob"])
        index.vocab_offsets = _narrow(arrays["vocab_offsets"])
        index.indptr = _narrow(arrays["indptr"])
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


# ------------------------------------------------------- segmented sidecar
#
# The monolithic sealed sidecar (one AES-GCM blob over the whole npz) costs a
# cold CLI one full decryption of ~145 MB and a sealed server its whole
# postings in private RAM. AFS2 fixes both: small arrays stay in one sealed
# header blob, the postings are split into SEG_POSTINGS chunks that are each
# sealed separately, and the file is memory-mapped so ciphertext pages never
# count toward private memory. A query decrypts its header once and only the
# segments that actually hold postings for its terms.



def write_sealed_segments(path, index: "InRamBM25", vault, seg_postings: int = SEG_POSTINGS) -> None:
    """Write `index` as an AFS2 sidecar: sealed JSON/npz header + sealed segments.

    Layout: `SEG_MAGIC | u64 header_len | sealed_header | (u64 len | sealed_seg)*`.
    The segment plaintext is `pos_part (4*n bytes) || tfs_part (2*n bytes)`, so
    a posting range inside a segment is two aligned byte slices.
    """
    total = int(len(index.flat_pos))
    pos_bytes = np.ascontiguousarray(index.flat_pos).tobytes()
    tfs_bytes = np.ascontiguousarray(index.flat_tfs).tobytes()
    # Postings are highly repetitive; zstd cuts the sealed sidecar roughly in
    # half before encryption. The header records the choice so old sidecars
    # keep loading unchanged.
    compressor = _zstd.ZstdCompressor(level=3) if _zstd is not None else None
    sealed_segments: list[bytes] = []
    seg_postings_list: list[int] = []
    for lo in range(0, total, seg_postings):
        hi = min(lo + seg_postings, total)
        blob = pos_bytes[lo * 4 : hi * 4] + tfs_bytes[lo * 2 : hi * 2]
        if compressor is not None:
            blob = compressor.compress(blob)
        sealed_segments.append(vault.seal(blob))
        seg_postings_list.append(hi - lo)
    header = {
        "ids": _narrow(np.asarray(index.ids)),
        "doc_len": index.doc_len,
        "avgdl": np.asarray([float(index.avgdl)], dtype=np.float64),
        "vocab_blob": np.frombuffer(index.vocab_blob, dtype=np.uint8),
        "vocab_offsets": _narrow(np.asarray(index.vocab_offsets)),
        "indptr": _narrow(np.asarray(index.indptr)),
        "fingerprint": np.asarray(index.fingerprint or (-1, -1), dtype=np.int64),
        "seg_postings": np.asarray(seg_postings_list, dtype=np.int64),
        "seg_sealed": np.asarray([len(s) for s in sealed_segments], dtype=np.int64),
        "compressed": np.asarray([1 if compressor is not None else 0], dtype=np.int64),
    }
    bio = io.BytesIO()
    np.savez(bio, **header)
    header_plain = bio.getvalue()
    # The header is ~23 MB of vocab blob + CSR offsets; zstd cuts it roughly
    # in half before sealing, so every process open decrypts and holds less.
    # The loader recognizes the frame by its magic (old sidecars stay valid).
    if _zstd is not None:
        header_plain = _zstd.ZstdCompressor(level=3).compress(header_plain)
    sealed_header = vault.seal(header_plain)
    with open(path, "wb") as out:
        out.write(SEG_MAGIC)
        out.write(struct.Struct("<Q").pack(len(sealed_header)))
        out.write(sealed_header)
        for sealed in sealed_segments:
            out.write(struct.Struct("<Q").pack(len(sealed)))
            out.write(sealed)


class _SegLRU:
    """Bounded store of decrypted segment bytes, keyed by segment index."""

    def __init__(self, max_bytes: int = SEG_CACHE_BYTES) -> None:
        self._items: OrderedDict[int, bytes] = OrderedDict()
        self._bytes = 0
        self._max = max_bytes
        self._lock = threading.Lock()

    def get(self, key: int) -> bytes | None:
        with self._lock:
            blob = self._items.get(key)
            if blob is not None:
                self._items.move_to_end(key)
            return blob

    def put(self, key: int, blob: bytes) -> None:
        with self._lock:
            if key in self._items:
                return
            self._items[key] = blob
            self._bytes += len(blob)
            # Evict oldest first until the cache is genuinely bounded. The old
            # `len(self._items) > 1` guard let one oversized last segment stay
            # resident forever, defeating the byte cap. The just-added segment
            # is evicted only if it alone exceeds the cap; the caller still
            # holds `blob`, so the current query stays correct.
            while self._bytes > self._max and self._items:
                _, evicted = self._items.popitem(last=False)
                self._bytes -= len(evicted)


class SealedSegmentBM25(InRamBM25):
    """InRamBM25 whose postings come from an AFS2 sidecar on demand.

    The small arrays are decrypted once at open (~10 MB); `_postings`
    decrypts only the segments a term touches and keeps them in a bounded
    LRU. `search` and `fingerprint` behave exactly like the in-RAM parent, so
    the store can hold either without caring.
    """

    __slots__ = ("_map", "_file", "_vault", "_seg_postings", "_seg_sealed",
                 "_segs_start", "_lru", "_compressed", "_decompressor")

    def __init__(self, header_arrays: dict, map_, file, vault, segs_start: int) -> None:
        self.ids = _narrow(header_arrays["ids"])
        self.doc_len = header_arrays["doc_len"]
        avg = header_arrays.get("avgdl")
        self.avgdl = float(avg[0]) if avg is not None and avg.size else 1.0
        self.vocab_blob = bytes(header_arrays["vocab_blob"])
        self.vocab_offsets = _narrow(header_arrays["vocab_offsets"])
        self.indptr = _narrow(header_arrays["indptr"])
        fp = header_arrays.get("fingerprint")
        self.fingerprint = (
            tuple(int(v) for v in fp)
            if fp is not None and fp.size and int(fp[0]) >= 0
            else None
        )
        self.flat_pos = None  # postings live on disk; slices materialise below
        self.flat_tfs = None
        self.built_in = 0.0
        self._map = map_
        self._file = file
        self._vault = vault
        self._seg_postings = [int(n) for n in header_arrays["seg_postings"]]
        self._seg_sealed = [int(n) for n in header_arrays["seg_sealed"]]
        self._segs_start = segs_start
        self._lru = _SegLRU()
        flag = header_arrays.get("compressed")
        self._compressed = bool(flag is not None and flag.size and int(flag[0]))
        self._decompressor = _zstd.ZstdDecompressor() if self._compressed and _zstd else None

    def close(self) -> None:
        mapping, handle = getattr(self, "_map", None), getattr(self, "_file", None)
        self._map = None
        self._file = None
        if mapping is not None:
            try:
                mapping.close()
            except (BufferError, OSError, ValueError):
                pass
        if handle is not None:
            try:
                handle.close()
            except (BufferError, OSError, ValueError):
                pass

    def _segment_bytes(self, seg: int) -> bytes:
        cached = self._lru.get(seg)
        if cached is not None:
            return cached
        prefix = self._segs_start + seg * 8 + sum(self._seg_sealed[:seg])
        sealed_len = self._seg_sealed[seg]
        raw = self._map[prefix : prefix + 8 + sealed_len]
        (length,) = struct.Struct("<Q").unpack(raw[:8])
        blob = self._vault.open_bytes(raw[8 : 8 + length])
        if self._compressed and self._decompressor is not None:
            blob = self._decompressor.decompress(blob)
        self._lru.put(seg, blob)
        return blob

    def _postings(self, index: int) -> tuple[np.ndarray, np.ndarray]:
        lo, hi = int(self.indptr[index]), int(self.indptr[index + 1])
        if hi <= lo:
            return np.empty(0, dtype=np.int32), np.empty(0, dtype=np.uint16)
        # Segment k covers postings [starts[k], starts[k+1]); the query range
        # is sliced out of every segment it touches. One term usually sits in
        # one segment; a 200-term prefix expansion may fan out wider.
        pos_parts, tfs_parts = [], []
        base = 0
        for seg, count in enumerate(self._seg_postings):
            start, end = base, base + count
            base = end
            if end <= lo:
                continue
            if start >= hi:
                break
            blob = self._segment_bytes(seg)
            s_lo, s_hi = max(lo, start) - start, min(hi, end) - start
            pos_parts.append(np.frombuffer(blob[s_lo * 4 : s_hi * 4], dtype=np.int32))
            tfs_parts.append(
                np.frombuffer(blob[count * 4 + s_lo * 2 : count * 4 + s_hi * 2],
                              dtype=np.uint16)
            )
        if len(pos_parts) == 1:
            return pos_parts[0], tfs_parts[0]
        return (
            np.concatenate(pos_parts),
            np.concatenate(tfs_parts),
        )

    @classmethod
    def open(cls, path, vault) -> "SealedSegmentBM25 | None":
        """Open an AFS2 sidecar, decrypting only the header. None on any
        mismatch — the caller falls back to the monolithic loader."""
        file = None
        map_ = None
        try:
            file = open(path, "rb")  # noqa: SIM115 — lives with the index
            map_ = mmap.mmap(file.fileno(), 0, access=mmap.ACCESS_READ)
            if map_[:8] != SEG_MAGIC:
                map_.close()
                file.close()
                return None
            (hdr_len,) = struct.Struct("<Q").unpack(map_[8:16])
            # memoryview slice: no copy of the ~30 MB ciphertext header
            plain = vault.open_bytes(memoryview(map_)[16 : 16 + hdr_len])
            if _zstd is not None and plain[:4] == b"\x28\xb5\x2f\xfd":
                plain = _zstd.ZstdDecompressor().decompress(plain)
            data = np.load(io.BytesIO(plain), allow_pickle=False)
            header_arrays = {name: data[name] for name in data.files}
            data.close()
            # Both buffers are transient: without the dels the ~32 MB plaintext
            # plus its ciphertext source stay reachable until the constructor
            # returns, and the first-request RSS peak doubles for nothing.
            del plain, data
            return cls(header_arrays, map_, file, vault, 16 + hdr_len)
        except Exception:
            if map_ is not None:
                try:
                    map_.close()
                except Exception:
                    pass
            if file is not None:
                try:
                    file.close()
                except Exception:
                    pass
            return None
