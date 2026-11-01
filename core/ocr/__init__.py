"""OCR layer.

Screenshots go through preprocessing (invert dark themes, upscale small text)
and then through whichever engine is present. The text lands in the same chunks
table as documents, which is what lets one query return a PDF and a screenshot
side by side.

The chain is tiered because no single engine reads every script, and because the
fast engine is 100× faster than the slow one:

* every installed Windows language pack runs first (~10 ms each);
* if the best of those is implausible for the amount of ink in the image, the
  bundled PP-OCRv6 `multi` model runs (~1 s, 53 languages);
* if that also fails, the downloaded per-script models are tried in order of
  how common the script is in screenshots (Korean, Arabic, Cyrillic, …).

See `script.py` for why the "did it fail" test is measured rather than guessed.
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
    coverage,
    fallback_engines,
    get_engine,
    primary_engines,
    script_engine,
)

__all__ = [
    "OcrResult",
    "OcrEngine",
    "OcrUnavailable",
    "available_engines",
    "cjk_coverage",
    "coverage",
    "engine_report",
    "get_engine",
    "ocr_image",
    "reset_fallbacks",
    "result_dict",
    "supported_languages",
]


class OcrUnavailable(RuntimeError):
    pass


_CACHE: dict[str, OcrEngine] = {}
_REPORT: dict[str, dict] = {}
# How many extra engines an unreadable image may be sent through. Each one is
# about a second of CPU, so an image with no text at all must not walk the whole
# table looking for a script it does not contain.
DEFAULT_FALLBACKS = 4

# ...and a whole folder of photographs must not pay that second four times over.
# The expensive tier switches itself off after this many consecutive attempts
# that changed nothing: a photo library stops costing anything after ~10 s, and
# a library of Japanese screenshots — where every attempt succeeds — never hits
# the limit at all.
GIVE_UP_AFTER = 8


class _Patience:
    def __init__(self, limit: int = GIVE_UP_AFTER) -> None:
        self.limit = limit
        self.misses = 0

    def reset(self) -> None:
        self.misses = 0

    def note(self, helped: bool) -> None:
        self.misses = 0 if helped else self.misses + 1

    def spent(self) -> bool:
        return self.limit > 0 and self.misses >= self.limit


_patience = _Patience()


def reset_fallbacks() -> None:
    """Called by the indexer at the start of a run, so one bad folder does not
    silence OCR for the next one."""
    _patience.reset()


def engine_report(languages_hint: str = "rus+eng") -> dict:
    """What OCR can do here. Cached: building it imports both OCR packages.

    `/api/status` and `/api/ocr` both ask, and the boot screen asks before the
    window has painted — half a second of `import rapidocr` on every request is
    half a second of a splash screen that has not woken up yet.
    """
    cached = _REPORT.get(languages_hint)
    if cached is not None:
        return cached
    engines = available_engines(languages_hint)
    cover = coverage()
    report = {
        "available": [engine.name for engine in engines],
        "active": engines[0].name if engines else None,
        "languages": languages_hint,
        "cjk": cjk_coverage(),
        "coverage": cover,
        "supported_languages": cover["total"],
    }
    _REPORT[languages_hint] = report
    return report


def supported_languages() -> list[dict]:
    """The languages readable on this machine, for the boot screen and `/api/ocr`."""
    return coverage()["languages"]


def ocr_image(
    path: str | Path,
    languages_hint: str = "rus+eng",
    preferred: str | None = None,
    invert_dark: bool = True,
    cjk: bool = True,
    cjk_model: str | None = None,
    cjk_keys: str | None = None,
    fallbacks: int = DEFAULT_FALLBACKS,
) -> OcrResult:
    """Read text from an image, falling back to a wider model when needed."""
    image = load_image(path)
    if image is None:
        raise OcrUnavailable(f"cannot decode image: {path}")
    width, height = image.size

    result = _first_pass(image, languages_hint, preferred, invert_dark)
    if result is None:
        raise OcrUnavailable(
            "no OCR engine found: install a Windows language pack, Tesseract 5, "
            "or pip install rapidocr"
        )

    if not cjk or fallbacks <= 0:
        return result
    if not script.looks_unread(result.text, width, height):
        return result
    if _patience.spent():
        return result

    improved = False
    for engine in _fallback_chain(result.engine, fallbacks, cjk_model, cjk_keys):
        second = _run(engine, image, invert_dark)
        if second is None:
            _patience.note(False)
            continue
        if script.better(result.text, second.text):
            result = second
            improved = True
            _patience.note(True)
        else:
            _patience.note(False)
        if not script.looks_unread(result.text, width, height):
            break
    if not improved:
        _patience.note(False)
    return result


def _first_pass(image, languages_hint: str, preferred: str | None, invert_dark: bool):
    """Best of the fast engines. One pass each; the fastest script wins."""
    if preferred:
        engine = _cached(f"preferred:{preferred}:{languages_hint}", lambda: get_engine(preferred, languages_hint))
        return _run(engine, image, invert_dark) if engine is not None else None

    engines = _cached(
        f"primary:{languages_hint}", lambda: primary_engines(languages_hint)
    ) or []
    best: OcrResult | None = None
    for engine in engines:
        result = _run(engine, image, invert_dark)
        if result is None:
            continue
        if best is None or script.text_score(result.text) > script.text_score(best.text):
            best = result
        # A Windows pack that read the image confidently does not need the other
        # three packs, let alone a one-second model.
        if not script.looks_unread(best.text, *image.size):
            break
    return best


def _run(engine: OcrEngine | None, image, invert_dark: bool) -> OcrResult | None:
    if engine is None:
        return None
    prepared = preprocess_for_ocr(
        image,
        mode=getattr(engine, "preprocess_mode", "rgb"),
        invert_dark=invert_dark,
    )
    try:
        return engine.recognize(prepared)
    except Exception:
        return None


def _cached(key: str, build):
    if key in _CACHE:
        return _CACHE[key]
    value = build()
    _CACHE[key] = value
    return value


def _fallback_chain(
    already: str, limit: int, rec_model: str | None, rec_keys: str | None
) -> list[OcrEngine]:
    engines = _cached(f"fallback:{limit}", lambda: fallback_engines(limit=limit)) or []
    # `already` is either a plain name or `name:lang`, and an engine that already
    # failed must not be asked again.
    seen = {already}
    if rec_model:
        extra = _cached(f"cjk:{rec_model}:{rec_keys}", lambda: cjk_engines(rec_model, rec_keys))
        engines = list(extra or []) + list(engines)
    return [engine for engine in engines if getattr(engine, "label", engine.name) not in seen]


def result_dict(result: OcrResult) -> dict:
    return asdict(result)
