"""OCR layer.

Screenshots go through preprocessing (invert dark themes, upscale small text)
and then through whichever engine is present. The text lands in the same chunks
table as documents, which is what lets one query return a PDF and a screenshot
side by side.

Two tiers, because one engine cannot cover every script: the always-present
engine runs first, and if it clearly could not read the image a CJK-capable
engine gets a second attempt. See `script.py` for why that decision is measured
rather than guessed.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..images import load_image, preprocess_for_ocr
from . import script
from .engine import (
    OcrEngine,
    OcrResult,
    WindowsOcr,
    available_engines,
    cjk_coverage,
    cjk_engines,
    get_engine,
)

__all__ = [
    "OcrResult",
    "OcrEngine",
    "OcrUnavailable",
    "available_engines",
    "cjk_coverage",
    "engine_report",
    "get_engine",
    "ocr_image",
    "result_dict",
]


class OcrUnavailable(RuntimeError):
    pass


_CACHE: dict[str, OcrEngine] = {}


def engine_report(languages: str = "rus+eng") -> dict:
    engines = available_engines(languages)
    coverage = cjk_coverage()
    return {
        "available": [engine.name for engine in engines],
        "active": engines[0].name if engines else None,
        "languages": languages,
        "cjk": coverage,
    }


def ocr_image(
    path: str | Path,
    languages: str = "rus+eng",
    preferred: str | None = None,
    invert_dark: bool = True,
    cjk: bool = True,
    cjk_model: str | None = None,
    cjk_keys: str | None = None,
) -> OcrResult:
    """Read text from an image, falling back to a CJK engine when needed."""
    key = f"{preferred or 'auto'}:{languages}:{cjk}"
    engine = _CACHE.get(key) or get_engine(preferred, languages)
    if engine is None:
        raise OcrUnavailable(
            "no OCR engine found: install the Windows language pack, Tesseract 5, "
            "or pip install rapidocr-onnxruntime"
        )
    _CACHE[key] = engine

    image = load_image(path)
    if image is None:
        raise OcrUnavailable(f"cannot decode image: {path}")

    prepared = preprocess_for_ocr(
        image,
        mode=getattr(engine, "preprocess_mode", "rgb"),
        invert_dark=invert_dark,
    )
    result = engine.recognize(prepared)

    if not cjk:
        return result

    width, height = image.size
    if not script.looks_unread(result.text, width, height):
        return result

    fallback = _cjk_engine(cjk_model, cjk_keys)
    if fallback is None or fallback.name == result.engine:
        return result

    prepared = preprocess_for_ocr(
        image,
        mode=getattr(fallback, "preprocess_mode", "rgb"),
        invert_dark=invert_dark,
    )
    try:
        second = fallback.recognize(prepared)
    except Exception:
        return result
    if script.better(result.text, second.text):
        return second
    return result


def _cjk_engine(rec_model: str | None, rec_keys: str | None) -> OcrEngine | None:
    key = f"cjk:{rec_model or ''}:{rec_keys or ''}"
    if key in _CACHE:
        return _CACHE[key]
    engines = cjk_engines(rec_model, rec_keys)
    engine = engines[0] if engines else None
    if engine is not None:
        _CACHE[key] = engine
    return engine


def result_dict(result: OcrResult) -> dict:
    return asdict(result)
