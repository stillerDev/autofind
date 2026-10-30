"""Embedder factories.

Text: e5-small on ONNX when the weights are present, otherwise the
hashed-feature embedder. Visual: CLIP when exported, otherwise the layout
descriptor. Callers only see the same two methods, so the pipeline never
branches on which one is active.
"""

from __future__ import annotations

from pathlib import Path

from .hashing import HashingEmbedder
from .onnx_e5 import EmbedderUnavailable, OnnxE5
from .visual import ClipOnnx, LayoutEmbedder, get_visual_embedder

__all__ = [
    "get_embedder",
    "get_visual_embedder",
    "HashingEmbedder",
    "OnnxE5",
    "LayoutEmbedder",
    "ClipOnnx",
    "EmbedderUnavailable",
]


def get_embedder(prefer: str = "auto", model_dir: str | Path | None = None):
    if prefer in ("auto", "e5") and model_dir is not None:
        try:
            return OnnxE5(model_dir)
        except EmbedderUnavailable:
            if prefer == "e5":
                raise
    return HashingEmbedder()
