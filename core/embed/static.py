"""Static multilingual embeddings: a lookup table, not a neural net.

The idea is the model2vec recipe: a sentence embedding is (close enough to) the
weighted mean of its token embeddings, and the token embedding matrix of a
trained encoder is available without running the encoder at all. So
`tools/distill_static.py` copies the token embedding matrix out of e5-small
once — quantised to int8 with per-row scales, ~96 MB as an mmap — and from then
on a query costs a tokenize plus one mean over a few hundred rows: tens of
microseconds, no ONNX Runtime in the query path, and Russian is kept because
the source model was multilingual.

The trade against e5 is honest: mean-of-tokens loses the attention that lets a
transformer weigh context, so relevance is a notch lower. It stays far above
the hashing embedder and it is semantic — «кофемашина» still finds espresso
recipes. `--embedder e5` buys the transformer back for a per-query forward
pass; `auto` prefers this pack when it exists.
"""

from __future__ import annotations

import mmap
import struct
from pathlib import Path
from typing import Iterable

from ..lazynp import np

MAX_TOKENS = 512
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "

HEADER = struct.Struct("<8sII")
MAGIC = b"SFASTTBL"


class StaticUnavailable(RuntimeError):
    pass


class StaticEmbedder:
    name = "static-e5"
    dim = 384
    # Distilled from e5: semantic, so the vector channel earns its full share.
    rrf_weight = 1.0
    semantic = True

    def __init__(self, model_dir: str | Path) -> None:
        directory = Path(model_dir) / "static-e5"
        table_path = directory / "table.bin"
        tokenizer_path = directory / "tokenizer.json"
        if not table_path.exists() or not tokenizer_path.exists():
            raise StaticUnavailable(
                f"static pack not found in {directory} (expected table.bin + tokenizer.json); "
                "distill it with tools/distill_static.py after installing the e5 weights"
            )
        try:
            from tokenizers import Tokenizer  # type: ignore
        except ImportError as exc:
            raise StaticUnavailable(
                "the static pack needs the `tokenizers` package: pip install 'autofind[onnx]'"
            ) from exc

        raw = table_path.read_bytes()[: HEADER.size]
        magic, dim, vocab = HEADER.unpack(raw)
        if magic != MAGIC:
            raise StaticUnavailable(f"{table_path} is not a static embedding table")
        expected = HEADER.size + vocab * 4 + vocab * dim
        if table_path.stat().st_size < expected:
            raise StaticUnavailable(f"{table_path} is truncated")
        self.dim = dim

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.no_padding()
        # vocab here is the table's row count; tokenizer ids above it (rare
        # added tokens) are handled at lookup time.
        self.vocab = vocab

        self._file = open(table_path, "rb")  # noqa: SIM115 — lives with the embedder
        self._map = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
        offset = HEADER.size
        self._scales = np.ndarray((vocab,), dtype=np.float32, buffer=self._map, offset=offset)
        offset += vocab * 4
        self._rows = np.ndarray(
            (vocab, dim), dtype=np.int8, buffer=self._map, offset=offset
        )

    def encode_passages(self, texts: Iterable[str]) -> np.ndarray:
        return self._encode([PASSAGE_PREFIX + text for text in texts])

    def encode_query(self, text: str) -> np.ndarray:
        return self._encode([QUERY_PREFIX + text])[0]

    def close(self) -> None:
        """Release the table mapping. Windows cannot delete a mapped file, so
        tests and short-lived callers must close; a server-side embedder lives
        for the process and never bothers."""
        matrix, scales = getattr(self, "_rows", None), getattr(self, "_scales", None)
        handle = getattr(self, "_map", None)
        if matrix is not None:
            self._rows = None
        if scales is not None:
            self._scales = None
        if handle is not None:
            self._map = None
            try:
                handle.close()
            except (BufferError, OSError, ValueError):
                pass
        if getattr(self, "_file", None) is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "StaticEmbedder":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        batch = self.tokenizer.encode_batch(texts)
        out = np.zeros((len(batch), self.dim), dtype=np.float32)
        for position, item in enumerate(batch):
            ids = np.asarray(item.ids, dtype=np.int64)
            # Specials (<s>, </s>, padding) carry framing, not meaning; e5's
            # mean pooling masks them the same way.
            keep = np.asarray(item.special_tokens_mask) == 0
            if not keep.any():
                continue
            ids = ids[keep]
            inside = ids < self.vocab
            if not inside.any():
                continue
            ids = ids[inside]
            # Dequantise exactly the rows this text needs: int8 row -> float,
            # scaled back by the row's max-abs from distillation time.
            vectors = self._rows[ids].astype(np.float32) * self._scales[ids, None]
            out[position] = vectors.mean(axis=0)
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return (out / np.clip(norms, 1e-6, None)).astype(np.float32)
