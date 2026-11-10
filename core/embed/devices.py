"""Which execution providers this onnxruntime build can actually offer.

OCR and embedding are the two expensive stages of a first index, and both run
on onnxruntime, so the GPU question is answered in exactly one place. The
Windows story is DirectML: it ships inside the ``onnxruntime-directml`` wheel,
works on any DX12 adapter (every recent Nvidia, AMD and Intel card) and needs
no CUDA/cuDNN version matching. The CUDA provider is honoured when the GPU
wheel is installed; CPU is always the last resort and always present.

A provider listed here can still fail when a session is actually built — a
driver quirk, missing ops, exhausted VRAM. Every caller must therefore treat
GPU as a preference: try it, and fall back to the CPU on the first failure.
"""

from __future__ import annotations

GPU_ORDER = ("CUDAExecutionProvider", "DmlExecutionProvider")


def _available_providers() -> list[str]:
    try:
        import onnxruntime as ort
    except ImportError:
        return []
    try:
        return list(ort.get_available_providers())
    except Exception:
        return []


def gpu_provider() -> str | None:
    """The best GPU provider this build advertises, or None for CPU-only."""
    available = _available_providers()
    for name in GPU_ORDER:
        if name in available:
            return name
    return None


def session_providers() -> list[str]:
    """Providers for ``InferenceSession``, best first; CPU always last."""
    gpu = gpu_provider()
    return ([gpu] if gpu else []) + ["CPUExecutionProvider"]
