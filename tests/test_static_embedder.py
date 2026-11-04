"""The static (distilled) embedder: pack format, lookup math, factory order."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

try:
    from tokenizers import Tokenizer  # type: ignore

    HAS_TOKENIZERS = True
except ImportError:
    HAS_TOKENIZERS = False

from core.embed import get_embedder
from core.embed.static import HEADER, MAGIC, StaticEmbedder, StaticUnavailable


def write_pack(directory: Path, tokens: list[str], matrix: np.ndarray) -> None:
    """Build a pack the way tools/distill_static.py does."""
    dim = matrix.shape[1]
    scales = np.abs(matrix).max(axis=1)
    scales[scales == 0] = 1.0
    quantised = np.clip(np.rint(matrix / scales[:, None]), -127, 127).astype(np.int8)
    pack = directory / "static-e5"
    pack.mkdir(parents=True, exist_ok=True)
    with open(pack / "table.bin", "wb") as handle:
        handle.write(HEADER.pack(MAGIC, dim, len(tokens)))
        handle.write(scales.tobytes())
        handle.write(quantised.tobytes())
    (pack / "tokenizer.json").write_text(
        json.dumps(
            {
                "version": "1.0",
                "truncation": None,
                "padding": None,
                "added_tokens": [],
                "normalizer": None,
                "pre_tokenizer": {"type": "Whitespace"},
                "post_processor": None,
                "decoder": None,
                "model": {
                    "type": "WordLevel",
                    "unk_token": "[UNK]",
                    "vocab": {"[UNK]": len(tokens), **{token: index for index, token in enumerate(tokens)}},
                },
            }
        ),
        encoding="utf-8",
    )


class PackCase(unittest.TestCase):
    """Temporary dir removed only after the embedder closed its mmap: Windows
    cannot delete a file that is still mapped."""

    def make(self, tokens: list[str], matrix: np.ndarray) -> StaticEmbedder:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        write_pack(Path(tmp), tokens, matrix)
        embedder = StaticEmbedder(tmp)
        self.addCleanup(embedder.close)
        return embedder


@unittest.skipUnless(HAS_TOKENIZERS, "the static pack needs the `tokenizers` package")
class StaticEmbedderTests(PackCase):
    def test_lookup_matches_the_distilled_matrix(self):
        # Two orthogonal tokens; the embedding of a one-token text must be that
        # token's row, dequantised and renormalised.
        matrix = np.array([[1.0] + [0.0] * 383, [0.0, 2.0] + [0.0] * 382], dtype=np.float32)
        embedder = self.make(["alpha", "beta"], matrix)
        vector = embedder.encode_query("alpha")
        expected = matrix[0] / np.linalg.norm(matrix[0])
        np.testing.assert_allclose(vector, expected, atol=1e-5)
        self.assertEqual(embedder.name, "static-e5")
        self.assertTrue(embedder.semantic)

    def test_two_word_query_is_the_normalised_mean(self):
        matrix = np.array([[1.0] + [0.0] * 383, [0.0, 1.0] + [0.0] * 382], dtype=np.float32)
        embedder = self.make(["alpha", "beta"], matrix)
        vector = embedder.encode_query("alpha beta")
        expected = np.array([0.5, 0.5] + [0.0] * 382, dtype=np.float32)
        expected /= np.linalg.norm(expected)
        np.testing.assert_allclose(vector, expected, atol=1e-5)

    def test_unknown_words_degrade_to_zero_not_crash(self):
        matrix = np.array([[1.0] + [0.0] * 383], dtype=np.float32)
        embedder = self.make(["alpha"], matrix)
        vector = embedder.encode_query("ZZZunknown")
        self.assertEqual(float(np.linalg.norm(vector)), 0.0)

    def test_passages_batch_shape(self):
        matrix = np.eye(3, 384, dtype=np.float32)
        embedder = self.make(["alpha", "beta", "gamma"], matrix)
        out = embedder.encode_passages(["alpha", "beta gamma"])
        self.assertEqual(out.shape, (2, 384))
        np.testing.assert_allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-6)
        # A passage with no words has no direction: zero, not NaN.
        empty = embedder.encode_passages([""])
        self.assertEqual(float(np.abs(empty).max()), 0.0)


class FactoryOrderTests(unittest.TestCase):
    def test_static_pack_beats_e5_in_auto_but_flag_overrides(self):
        # No models here at all: auto falls through to hashing, and asking for
        # static explicitly must raise rather than silently degrade.
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(get_embedder("auto", tmp).name, "hashing-384")
            with self.assertRaises(StaticUnavailable):
                get_embedder("static", tmp)


if __name__ == "__main__":
    unittest.main()
