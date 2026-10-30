"""OCR layer.

Screenshots go through preprocessing (invert dark themes, upscale small text)
and then through whichever engine is present. The text lands in the same chunks
table as documents, which is what lets one query return a PDF and a screenshot
side by side.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from ..images import load_image, preprocess_for_ocr
from .engine import OcrEngine, OcrResult, available_engines, get_engine

__all__ = [
    "OcrResult",
    "OcrEngine",
    "OcrUnavailable",
    "available_engines",
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
    return {
        "available": [engine.name for engine in engines],
        "active": engines[0].name if engines else None,
        "languages": languages,
    }


def ocr_image(
    path: str | Path,
    languages: str = "rus+eng",
    preferred: str | None = None,
    invert_dark: bool = True,
) -> OcrResult:
    key = f"{preferred or 'auto'}:{languages}"
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
    return engine.recognize(prepared)


def result_dict(result: OcrResult) -> dict:
    return asdict(result)
