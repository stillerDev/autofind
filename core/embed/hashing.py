"""Deterministic hashed-feature embedder.

This is the fallback named in the plan ("TF-IDF как базовый уровень"): word
features plus character 4-grams hashed into 384 dimensions, sublinear term
weighting, L2 normalised. It is not semantic, but it is dependency-free,
stable across runs and handles Russian morphology through the char n-grams.
"""

from __future__ import annotations

import re
import zlib
from typing import Iterable

import numpy as np

DIM = 384
TOKEN_RE = re.compile(r"\w+", re.UNICODE)
NGRAM = 4
NGRAM_WEIGHT = 0.35


class HashingEmbedder:
    name = "hashing-384"
    dim = DIM
    # Weight of this channel inside the RRF fusion. Hashed features mostly
    # duplicate what BM25 already does, only worse, so the channel is damped:
    # measured on the 8.4k-file corpus, an equal weight cost ~0.04 MRR.
    rrf_weight = 0.3
    # Not a semantic model: it cannot connect «кофемашина» to an espresso recipe.
    # Callers use this to decide whether a zero-lexical query should fall back to
    # the vector channel at all, or honestly return nothing.
    semantic = False

    def encode_passages(self, texts: Iterable[str]) -> np.ndarray:
        return np.vstack([self._vector(text) for text in texts]) if texts else np.zeros((0, DIM), np.float32)

    def encode_query(self, text: str) -> np.ndarray:
        return self._vector(text)

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(DIM, dtype=np.float32)
        for feature, weight in _features(text).items():
            digest = zlib.crc32(feature.encode("utf-8"))
            index = digest % DIM
            sign = 1.0 if (digest >> 20) & 1 else -1.0
            vector[index] += sign * weight
        vector = np.sign(vector) * np.log1p(np.abs(vector))
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector.astype(np.float32)


def _features(text: str) -> dict[str, float]:
    features: dict[str, float] = {}
    for token in TOKEN_RE.findall(text.lower()):
        if len(token) < 2:
            continue
        features[token] = features.get(token, 0.0) + 1.0
        if len(token) >= NGRAM + 1:
            for start in range(len(token) - NGRAM + 1):
                gram = token[start:start + NGRAM]
                features[gram] = features.get(gram, 0.0) + NGRAM_WEIGHT
    return features
