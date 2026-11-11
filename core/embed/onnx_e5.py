"""multilingual-e5-small on ONNX Runtime (int8).

Drop the quantised export into <AUTOFIND_HOME>/models/e5-small/ as model.onnx
and tokenizer.json and this embedder takes over automatically. Without those
files the factory falls back to the hashed-feature embedder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..lazynp import np

from .devices import session_providers

MAX_TOKENS = 512
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "
# Rows per session run: big enough to amortise the GPU/CPU round trip, small
# enough that one huge document cannot spike the working set.
_BATCH = 32


class EmbedderUnavailable(RuntimeError):
    pass


class OnnxE5:
    name = "e5-small-int8"
    dim = 384
    # A real semantic model earns an equal share of the fusion.
    rrf_weight = 1.0
    semantic = True

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
                "onnxruntime and tokenizers are required for e5: pip install 'autofind[onnx]'"
            ) from exc

        options = ort.SessionOptions()
        options.log_severity_level = 3
        # GPU when the build offers one (see devices.py): a forward pass that
        # took ~40 ms of CPU per batch drops to a few. intra_op threads stay at
        # one on the GPU so the model and the rest of the process do not fight
        # for the same cores. A provider that advertises itself can still fail
        # at session build time — then the CPU run is the fallback, not a crash.
        providers = session_providers()
        options.intra_op_num_threads = 0 if providers == ["CPUExecutionProvider"] else 1
        try:
            self.session = ort.InferenceSession(str(model_path), options, providers=providers)
        except Exception:
            if providers == ["CPUExecutionProvider"]:
                raise
            options.intra_op_num_threads = 0
            providers = ["CPUExecutionProvider"]
            self.session = ort.InferenceSession(str(model_path), options, providers=providers)
        self.provider = providers[0]
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
        # Similar-length texts travel together: padding a 40-token chunk to a
        # 512-token sibling burns real compute on every row, and running a
        # whole 400-chunk document through one session call spikes memory by
        # the same amount. Buckets sorted by length keep both costs near zero
        # and the mean pooling does not care about the order anyway.
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for start in range(0, len(order), _BATCH):
            part = order[start:start + _BATCH]
            batch = self.tokenizer.encode_batch([texts[i] for i in part])
            ids = np.array([item.ids for item in batch], dtype=np.int64)
            mask = np.array([item.attention_mask for item in batch], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feeds)[0]
            weights = mask[..., None].astype(np.float32)
            pooled = (hidden * weights).sum(axis=1) / np.clip(weights.sum(axis=1), 1e-6, None)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            out[part] = (pooled / np.clip(norms, 1e-6, None)).astype(np.float32)
        return out
