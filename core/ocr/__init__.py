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

import threading
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
    "WindowsOcr",
    "available_engines",
    "cjk_coverage",
    "coverage",
    "engine_report",
    "get_engine",
    "ocr_image",
    "reset_fallbacks",
    "result_dict",
    "script_engine",
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


# One counter per worker, not one for the whole process: the indexer runs
# recognizers in parallel, and a shared miss counter lets N simultaneous
# unreadable images exhaust GIVE_UP_AFTER N times too early — silently turning
# the expensive tier off for the rest of the run. Every counter is registered so
# a reset at the start of a run reaches workers that have not been created yet
# as well as those that have already exited.
_patience_local = threading.local()
_PATIENCES: list[_Patience] = []
_PATIENCES_LOCK = threading.Lock()


def _patience() -> _Patience:
    """The calling thread's fallback budget, created on first use."""
    current = getattr(_patience_local, "counter", None)
    if current is None:
        current = _Patience()
        _patience_local.counter = current
        with _PATIENCES_LOCK:
            _PATIENCES.append(current)
    return current


def reset_fallbacks() -> None:
    """Called by the indexer at the start of a run, so one bad folder does not
    silence OCR for the next one. Resets every worker's counter, not just the
    caller's."""
    with _PATIENCES_LOCK:
        for counter in _PATIENCES:
            counter.reset()


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
    # Indra's status is its own card: it depends on a downloaded pack, not on
    # the OCR packages the rest of the report is about.
    try:
        from . import indra

        indra_card = indra.indra_status()
    except Exception:
        indra_card = {"available": False, "problem": "indra module failed to load"}
    report = {
        "available": [engine.name for engine in engines],
        "active": engines[0].name if engines else None,
        "languages": languages_hint,
        "cjk": cjk_coverage(),
        "coverage": cover,
        "indra": indra_card,
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
    """Read text from an image file, falling back to a wider model when needed."""
    image = load_image(path)
    if image is None:
        raise OcrUnavailable(f"cannot decode image: {path}")
    return ocr_page(
        image,
        languages_hint,
        preferred,
        invert_dark,
        cjk,
        cjk_model,
        cjk_keys,
        fallbacks,
    )


def ocr_page(
    image,
    languages_hint: str = "rus+eng",
    preferred: str | None = None,
    invert_dark: bool = True,
    cjk: bool = True,
    cjk_model: str | None = None,
    cjk_keys: str | None = None,
    fallbacks: int = DEFAULT_FALLBACKS,
) -> OcrResult:
    """Read text from a decoded PIL image — the entry scanned-PDF pages take
    (the page is rendered to a bitmap, so there is no file to load)."""
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
    budget = _patience()
    if budget.spent():
        return result

    chain = _fallback_chain(result.engine, fallbacks, cjk_model, cjk_keys)
    if not chain:
        # Nothing to try: one miss for this image.
        budget.note(False)
        return result
    for engine in chain:
        second = _run(engine, image, invert_dark)
        if second is None:
            budget.note(False)
            continue
        if script.better(result.text, second.text):
            result = second
            budget.note(True)
        else:
            budget.note(False)
        if not script.looks_unread(result.text, width, height):
            break
    # Every attempt above already noted its own verdict — noting again here
    # would count the same image twice and halve the give-up budget.
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
