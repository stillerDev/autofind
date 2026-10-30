"""multilingual-e5-small on ONNX Runtime (int8).

Drop the quantised export into <SEMFIND_HOME>/models/e5-small/ as model.onnx
and tokenizer.json and this embedder takes over automatically. Without those
files the factory falls back to the hashed-feature embedder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import numpy as np

MAX_TOKENS = 512
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "


class EmbedderUnavailable(RuntimeError):
    pass


class OnnxE5:
    name = "e5-small-int8"
    dim = 384

    def __init__(self, model_dir: str | Path) -> None:
        directory = Path(model_dir)
        model_path = directory / "model.onnx"
        tokenizer_path = directory / "tokenizer.json"
        if not model_path.exists() or not tokenizer_path.exists():
            raise EmbedderUnavailable(
                f"e5-small not found in {directory} (expected model.onnx + tokenizer.json)"
            )
        try:
            import onnxruntime as ort  # type: ignore
            from tokenizers import Tokenizer  # type: ignore
        except ImportError as exc:
            raise EmbedderUnavailable(
                "onnxruntime and tokenizers are required for e5: pip install 'semfind[onnx]'"
            ) from exc

        options = ort.SessionOptions()
        options.intra_op_num_threads = 0
        options.log_severity_level = 3
        self.session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=MAX_TOKENS)
        self.tokenizer.enable_padding()
        self.input_names = {item.name for item in self.session.get_inputs()}

    def encode_passages(self, texts: Iterable[str]) -> np.ndarray:
        return self._encode([PASSAGE_PREFIX + text for text in texts])

    def encode_query(self, text: str) -> np.ndarray:
        return self._encode([QUERY_PREFIX + text])[0]

    def _encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        batch = self.tokenizer.encode_batch(texts)
        ids = np.array([item.ids for item in batch], dtype=np.int64)
        mask = np.array([item.attention_mask for item in batch], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feeds)[0]
        weights = mask[..., None].astype(np.float32)
        pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-6, None)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / np.clip(norms, 1e-6, None)).astype(np.float32)
