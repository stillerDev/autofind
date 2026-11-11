"""Embedder factories.

Text: e5-small on ONNX when the weights are present; else the static pack
distilled from e5 (a lookup table, tens of microseconds per query) when it has
been built; otherwise the hashed-feature embedder. Visual: CLIP when exported,
otherwise the layout descriptor. Callers only see the same two methods, so the
pipeline never branches on which one is active.
"""

from __future__ import annotations

from pathlib import Path

from .hashing import HashingEmbedder
from .onnx_e5 import EmbedderUnavailable, OnnxE5
from .static import StaticEmbedder, StaticUnavailable

__all__ = [
    "get_embedder",
    "get_visual_embedder",
    "HashingEmbedder",
    "OnnxE5",
    "StaticEmbedder",
    "LayoutEmbedder",
    "ClipOnnx",
    "EmbedderUnavailable",
    "StaticUnavailable",
]


def get_visual_embedder(model_dir=None):
    # Imported lazily so a text-only search (`autofind search`) does not pay
    # Pillow's import cost. The visual stack is only needed for screenshot
    # indexing and image similarity.
    from .visual import ClipOnnx, LayoutEmbedder, get_visual_embedder as _get

    globals()["ClipOnnx"] = ClipOnnx
    globals()["LayoutEmbedder"] = LayoutEmbedder
    return _get(model_dir)


def get_embedder(prefer: str = "auto", model_dir: str | Path | None = None):
    # `auto` prefers the static pack when it exists: it is distilled from e5,
    # so the quality loss is a notch, while the per-query cost drops from a
    # forward pass to a table lookup. `--embedder e5` buys the transformer
    # back for whoever wants the last bit of relevance.
    if prefer in ("auto", "static") and model_dir is not None:
        try:
            return StaticEmbedder(model_dir)
        except StaticUnavailable:
            if prefer == "static":
                raise
    if prefer in ("auto", "e5") and model_dir is not None:
        try:
            return OnnxE5(model_dir)
        except EmbedderUnavailable:
            if prefer == "e5":
                raise
    return HashingEmbedder()
