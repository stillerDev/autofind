"""File-level binary vectors: one bit per dimension, 48 bytes per file.

Since schema v9 a document has exactly one vector, and it is stored as the sign
of each dimension rather than as a float. 384 dimensions cost 48 bytes, so the
whole vector layer of a 14 000-file index is 666 KB instead of the 116 MB the
per-chunk i8 blobs took — and because there is one row per file, the search is
a plain matvec over 14 000 rows instead of a scan over 300 000 chunks.

The trade is deliberate: magnitudes are dropped, so two passages that differ
only in how strongly they lean on a dimension become the same vector. On this
corpus that costs 2-4% recall against float32 — kensaku's `binary` mode makes
the same trade — and the lexical channel carries the exact matches anyway.

Scoring is asymmetric: the stored vectors are ±1 and the *query* stays float32,
so `matrix @ query` is the dot product of the query with the sign pattern.
Binarising the query too would only lose information without saving a byte.
"""

from __future__ import annotations

import numpy as np

BITS_PER_BYTE = 8


def to_bits(vector: np.ndarray) -> bytes:
    """Pack a float vector into sign bits, MSB first (numpy's bit order)."""
    return np.packbits(np.asarray(vector) > 0).tobytes()


def from_bits(blob: bytes, dim: int) -> np.ndarray:
    """One stored vector as a ±1 float32 row."""
    bits = np.unpackbits(np.frombuffer(blob, dtype=np.uint8), count=dim)
    return bits.astype(np.float32) * 2.0 - 1.0


def matrix(blobs: list[bytes], dim: int) -> np.ndarray:
    """A (rows, dim) ±1 float32 matrix from packed rows, in one numpy pass.

    Unpacking row by row costs a Python call per file; the whole blob set is
    one contiguous buffer, so numpy can do it as a single 2-D operation.
    """
    if not blobs:
        return np.zeros((0, dim), dtype=np.float32)
    raw = np.frombuffer(b"".join(blobs), dtype=np.uint8).reshape(len(blobs), -1)
    bits = np.unpackbits(raw, axis=1, count=dim)
    # In place: `bits * 2 - 1` allocates two 154 MB temporaries at 100k files
    # and measured at 120 ms, against 60 ms for the same result written into
    # one buffer. The matrix is read-only afterwards, so mutating it is safe.
    matrix = bits.astype(np.float32)
    matrix *= 2.0
    matrix -= 1.0
    return matrix


def mean_pool(vectors: np.ndarray) -> np.ndarray:
    """One document vector from its chunk vectors: the mean direction.

    The mean of unit-length passages points at the dominant topic of the
    document, which is what a file-level vector is for. A single chunk passes
    through unchanged so callers need not special-case it.
    """
    array = np.asarray(vectors, dtype=np.float32)
    if array.ndim == 1:
        return array
    if array.shape[0] == 0:
        return np.zeros(array.shape[1], dtype=np.float32)
    return array.mean(axis=0)


def build(conn, dim: int) -> tuple[np.ndarray, np.ndarray]:
    """(file ids, ±1 matrix) for every stored file vector of this dimension."""
    rows = conn.execute(
        "SELECT file_id, bits FROM file_vectors WHERE dim = ? ORDER BY file_id",
        (int(dim),),
    ).fetchall()
    if not rows:
        return np.zeros(0, dtype=np.int64), np.zeros((0, dim), dtype=np.float32)
    ids = np.array([int(row["file_id"]) for row in rows], dtype=np.int64)
    return ids, matrix([row["bits"] for row in rows], int(dim))


def packed_size(dim: int) -> int:
    """Bytes one packed vector occupies, padding included."""
    return (dim + BITS_PER_BYTE - 1) // BITS_PER_BYTE
