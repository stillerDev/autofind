"""Embedder factories.

Text: e5-small on ONNX when the weights are present; else the static pack
distilled from e5 (a lookup table, tens of microseconds per query) when it has
been built; otherwise the hashed-feature embedder. Visual: CLIP when exported,
otherwise the layout descriptor. Callers only see the same two methods, so the
pipeline never branches on which one is active.
"""

from __future__ import annotations

import atexit
import os
import threading
from pathlib import Path

# Building the static pack parses a ~17 MB tokenizer and mmaps a ~96 MB table.
# That is a one-time cost: the embedder is stateless per query, so servers and
# repeated CLI calls must share one instance instead of paying it every request.
_EMBEDDER_CACHE: dict[tuple[str, str], object] = {}
_EMBEDDER_LOCK = threading.Lock()

from .hashing import HashingEmbedder
from .onnx_e5 import EmbedderUnavailable, OnnxE5
from .static import StaticEmbedder, StaticUnavailable

__all__ = [
    "ClipOnnx",
    "EmbedderUnavailable",
    "HashingEmbedder",
    "LayoutEmbedder",
    "OnnxE5",
    "StaticEmbedder",
    "StaticUnavailable",
    "close_embedders",
    "get_embedder",
    "get_visual_embedder",
]


def get_visual_embedder(model_dir=None):
    # Imported lazily so a text-only search (`autofind search`) does not pay
    # Pillow's import cost. The visual stack is only needed for screenshot
    # indexing and image similarity.
    from .visual import ClipOnnx, LayoutEmbedder
    from .visual import get_visual_embedder as _get

    globals()["ClipOnnx"] = ClipOnnx
    globals()["LayoutEmbedder"] = LayoutEmbedder
    return _get(model_dir)


def get_embedder(prefer: str = "auto", model_dir: str | Path | None = None):
    # `auto` prefers the static pack when it exists: it is distilled from e5,
    # so the quality loss is a notch, while the per-query cost drops from a
    # forward pass to a table lookup. `--embedder e5` buys the transformer
    # back for whoever wants the last bit of relevance.
    # Explicit override for deployments that trade a notch of relevance for a
    # smaller resident set (the hashing embedder loads no 96 MB model).
    if prefer == "auto":
        prefer = os.environ.get("AUTOFIND_EMBEDDER") or prefer
    key = (prefer, str(model_dir) if model_dir is not None else "")
    cached = _EMBEDDER_CACHE.get(key)
    if cached is not None:
        return cached
    # Double-check under the lock: parallel first requests used to build several
    # 96 MB embedders, only one of which ever reached the cache (or got closed).
    with _EMBEDDER_LOCK:
        cached = _EMBEDDER_CACHE.get(key)
        if cached is not None:
            return cached
        if prefer in ("auto", "static") and model_dir is not None:
            try:
                built = StaticEmbedder(model_dir)
                _EMBEDDER_CACHE[key] = built
                return built
            except StaticUnavailable:
                if prefer == "static":
                    raise
        if prefer in ("auto", "e5") and model_dir is not None:
            try:
                # One directory per model (e5-small/, and visual's CLIP beside
                # it): matches onnx_e5's documented drop-in path and the pack
                # downloader in core/models.py, and lets two models coexist.
                built = OnnxE5(Path(model_dir) / "e5-small")
                _EMBEDDER_CACHE[key] = built
                return built
            except EmbedderUnavailable:
                if prefer == "e5":
                    raise
        built = HashingEmbedder()
        _EMBEDDER_CACHE[key] = built
        return built


def close_embedders() -> None:
    """Release cached embedders at shutdown (the mmap'd static table in
    particular) and leave the cache empty for the next process."""
    with _EMBEDDER_LOCK:
        cached = list(_EMBEDDER_CACHE.values())
        _EMBEDDER_CACHE.clear()
    for embedder in cached:
        close = getattr(embedder, "close", None)
        if close is not None:
            try:
                close()
            except (OSError, BufferError, ValueError):
                pass  # a failed release must not break process shutdown


atexit.register(close_embedders)
