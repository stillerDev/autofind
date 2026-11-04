"""OCR engines.

Three tiers, and the order is the whole design:

1. **Windows OCR** — ships with the OS, ~10 ms per screenshot, but it only reads
   the scripts whose language packs are installed. Every installed pack is
   tried, not just the display language: on this machine `en-US` reads Russian
   as `npl'1BeT`, and the `ru` pack reads it correctly, for the same 10 ms.
2. **rapidocr PP-OCRv6 `multi`** — bundled with the package, 53 languages
   (all Latin-script languages, Japanese, Chinese), ~1 s per screenshot.
3. **rapidocr PP-OCRv5 script models** — Korean, Arabic, Cyrillic, Devanagari,
   Greek, Thai, Tamil, Telugu. Downloaded on first use (~8 MB each), then ~1 s.

Tiers 2 and 3 only run when the tier below could not read the image at all, so
the common case stays at 10 ms and a Korean screenshot costs two seconds instead
of being indexed as garbage. `script.looks_unread` is the gate — see there for
why it is measured rather than guessed.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from PIL import Image

from . import languages
from .script import CJK_TAGS


@dataclass(slots=True)
class OcrResult:
    text: str
    engine: str
    confidence: float = 0.0
    lines: list[str] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


class OcrEngine(Protocol):
    name: str
    preprocess_mode: str

    def available(self) -> bool: ...

    def recognize(self, image: Image.Image) -> OcrResult: ...


class WindowsOcr:
    """Windows.Media.Ocr through the winrt bindings."""

    name = "windows-ocr"
    preprocess_mode = "rgb"

    def __init__(self, language: str | None = None) -> None:
        self.language = language
        self._engine = None
        self._error: str | None = None

    def available(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import winrt.windows.media.ocr  # noqa: F401
        except ImportError:
            return False
        return self._get_engine() is not None

    def _get_engine(self):
        if self._engine is not None or self._error is not None:
            return self._engine
        try:
            from winrt.windows.media.ocr import OcrEngine as WinOcrEngine
            from winrt.windows.globalization import Language
        except ImportError as exc:
            self._error = str(exc)
            return None

        engine = None
        if self.language:
            try:
                engine = WinOcrEngine.try_create_from_language(Language(self.language))
            except Exception:
                engine = None
            if engine is None:
                # Do not fall back to the profile languages here: asking for `ja`
                # and silently getting `en-US` makes `available()` lie, and the
                # CJK router would then "retry" with the engine that already
                # failed.
                available = [item.language_tag for item in WinOcrEngine.available_recognizer_languages]
                self._error = f"no recogniser for {self.language}; available: {available}"
                return None
        if engine is None:
            try:
                engine = WinOcrEngine.try_create_from_user_profile_languages()
            except Exception as exc:
                self._error = str(exc)
                return None
        if engine is None:
            available = [item.language_tag for item in WinOcrEngine.available_recognizer_languages]
            self._error = f"no recogniser for profile languages; available: {available}"
            return None
        self._engine = engine
        try:
            self.language = engine.recognizer_language.language_tag
        except Exception:
            pass
        return engine

    def recognize(self, image: Image.Image) -> OcrResult:
        engine = self._get_engine()
        if engine is None:
            raise RuntimeError(f"windows-ocr unavailable: {self._error}")
        buffer = io.BytesIO()
        image.convert("RGB").save(buffer, format="PNG")
        text = _run_async(_recognize_bytes(engine, buffer.getvalue()))
        lines = [line for line in text.splitlines() if line.strip()]
        return OcrResult(text=text.strip(), engine=self.name, confidence=0.0, lines=lines)


def windows_language_tags() -> list[str]:
    """Language packs with a Windows recogniser, most useful first."""
    if sys.platform != "win32":
        return []
    try:
        from winrt.windows.media.ocr import OcrEngine as WinOcrEngine
    except ImportError:
        return []
    try:
        tags = [item.language_tag for item in WinOcrEngine.available_recognizer_languages]
    except Exception:
        return []
    # The display language first: it is the one the user's own screenshots are
    # most likely to be in.
    try:
        from winrt.windows.media.ocr import OcrEngine as _Engine

        profile = _Engine.try_create_from_user_profile_languages()
        if profile is not None:
            preferred = profile.recognizer_language.language_tag
            tags.sort(key=lambda tag: (tag != preferred, tag))
    except Exception:
        tags.sort()
    return tags


def windows_engines(limit: int = 4) -> list[OcrEngine]:
    """One engine per installed language pack.

    Trying every pack costs about 10 ms each and is the cheapest way to widen
    coverage: a Russian screenshot on a Russian Windows should not be read by the
    English recogniser and then sent to a 1 s model to be fixed.
    """
    found: list[OcrEngine] = []
    for tag in windows_language_tags()[:limit]:
        engine = WindowsOcr(tag)
        if engine.available():
            found.append(engine)
    return found


async def _recognize_bytes(engine, png: bytes) -> str:
    from winrt.windows.graphics.imaging import BitmapDecoder
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    writer.write_bytes(png)
    await writer.store_async()
    await writer.flush_async()
    stream.seek(0)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()
    result = await engine.recognize_async(bitmap)
    return result.text


def _run_async(coroutine):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    # Called from a thread that already owns a loop: run in a private one.
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coroutine)
    finally:
        loop.close()


# ----------------------------------------------------------------- rapidocr


def _rapid_models_dir() -> Path | None:
    try:
        import rapidocr
    except ImportError:
        return None
    return Path(rapidocr.__file__).resolve().parent / "models"


def downloaded_models() -> set[str]:
    """Recognition models already on disk.

    `multi` is the PP-OCRv6 model that ships inside the wheel. The script models
    are 8 MB downloads, so they are reported only once they exist — the boot
    screen must not promise a language that would take ten seconds and a network
    round trip to honour.
    """
    directory = _rapid_models_dir()
    if directory is None:
        return set()
    found: set[str] = set()
    try:
        names = [path.name.lower() for path in directory.glob("*rec*.onnx")]
    except OSError:
        return set()
    for name in names:
        if name.startswith("pp-ocrv6_rec") or name.startswith("multi_"):
            found.add("multi")
        for model in languages.MODELS:
            if model != "multi" and name.startswith(f"{model}_"):
                found.add(model)
    # `en` and `ch` share the multi model; a bare v5 Chinese model also reads
    # Latin text well enough to count as `multi` coverage.
    if any(name.startswith("ch_pp-ocrv5_rec") for name in names):
        found.add("multi")
    return found


class RapidOcrMulti:
    """rapidocr 3.x with a per-script recognition model.

    The default (`multi`) is PP-OCRv6 and reads 53 languages including Japanese
    and Chinese. Everything else asks for a PP-OCRv5 model by name; rapidocr
    downloads it on first construction.
    """

    name = "rapidocr"
    preprocess_mode = "rgb"

    def __init__(self, lang: str = "multi", model_dir: Path | None = None) -> None:
        self.lang = lang
        self.model_dir = model_dir
        self._engine = None
        self._error: str | None = None

    @property
    def label(self) -> str:
        return f"{self.name}:{self.lang}"

    def available(self) -> bool:
        try:
            import rapidocr  # noqa: F401
        except ImportError:
            return False
        return self._error is None

    def _build(self):
        from rapidocr import ModelType, OCRVersion, RapidOCR

        params: dict = {"Global.log_level": "warning"}
        if self.lang != "multi":
            params.update(
                {
                    "Rec.lang_type": self.lang,
                    "Rec.ocr_version": OCRVersion.PPOCRV5,
                    "Rec.model_type": ModelType.MOBILE,
                }
            )
        if self.model_dir is not None:
            params["Global.model_root_dir"] = str(self.model_dir)
        return RapidOCR(params=params or None)

    def recognize(self, image: Image.Image) -> OcrResult:
        if self._engine is None:
            try:
                self._engine = self._build()
            except Exception as exc:
                self._error = str(exc)
                raise RuntimeError(f"rapidocr:{self.lang} unavailable: {exc}") from exc
        import numpy as np

        output = self._engine(np.asarray(image.convert("RGB")))
        lines = list(output.txts or [])
        scores = [float(score) for score in (output.scores or [])]
        confidence = sum(scores) / len(scores) if scores else 0.0
        return OcrResult(
            text="\n".join(lines), engine=self.label, confidence=confidence, lines=lines
        )


class RapidOcr:
    """The older `rapidocr_onnxruntime` package: Chinese + Latin only.

    Kept as the last resort for machines that have the old wheel and not the new
    one. It is slower and reads less than `RapidOcrMulti`, so it never wins a
    tie.
    """

    name = "rapidocr-legacy"
    preprocess_mode = "rgb"

    def __init__(self, rec_model: str | None = None, rec_keys: str | None = None) -> None:
        self.rec_model = rec_model
        self.rec_keys = rec_keys
        self._engine = None

    def available(self) -> bool:
        try:
            from rapidocr_onnxruntime import RapidOCR  # noqa: F401
        except ImportError:
            return False
        return True

    def _build(self):
        from rapidocr_onnxruntime import RapidOCR

        if self.rec_model:
            params = {"Rec.model_path": self.rec_model}
            if self.rec_keys:
                params["Rec.rec_keys_path"] = self.rec_keys
            try:
                return RapidOCR(params=params)
            except Exception:
                pass  # a broken custom model must not disable OCR entirely
        return RapidOCR()

    def recognize(self, image: Image.Image) -> OcrResult:
        if self._engine is None:
            self._engine = self._build()
        import numpy as np

        result, _ = self._engine(np.asarray(image.convert("RGB")))
        lines = [item[1] for item in (result or [])]
        scores = [float(item[2]) for item in (result or [])]
        confidence = sum(scores) / len(scores) if scores else 0.0
        return OcrResult(text="\n".join(lines), engine=self.name, confidence=confidence, lines=lines)


class TesseractCli:
    name = "tesseract-cli"
    preprocess_mode = "gray"

    def __init__(self, languages: str = "rus+eng") -> None:
        self.languages = languages

    def available(self) -> bool:
        return shutil.which("tesseract") is not None

    def recognize(self, image: Image.Image) -> OcrResult:
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        process = subprocess.run(
            ["tesseract", "stdin", "stdout", "-l", self.languages, "--psm", "3"],
            input=buffer.getvalue(),
            capture_output=True,
            timeout=120,
        )
        text = process.stdout.decode("utf-8", errors="replace").strip()
        lines = [line for line in text.splitlines() if line.strip()]
        return OcrResult(text=text, engine=self.name, lines=lines)


class PyTesseractOcr:
    name = "pytesseract"
    preprocess_mode = "gray"

    def __init__(self, languages: str = "rus+eng") -> None:
        self.languages = languages

    def available(self) -> bool:
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            return False
        return shutil.which("tesseract") is not None

    def recognize(self, image: Image.Image) -> OcrResult:
        import pytesseract

        text = pytesseract.image_to_string(image, lang=self.languages)
        lines = [line for line in text.splitlines() if line.strip()]
        return OcrResult(text=text.strip(), engine=self.name, lines=lines)


FACTORIES = (
    (WindowsOcr, False),
    (TesseractCli, True),
    (PyTesseractOcr, True),
    (RapidOcrMulti, False),
    (RapidOcr, False),
)


def available_engines(languages_hint: str = "rus+eng") -> list[OcrEngine]:
    found: list[OcrEngine] = []
    for factory, takes_languages in FACTORIES:
        try:
            engine = factory(languages_hint) if takes_languages else factory()
            if engine.available():
                found.append(engine)
        except Exception:
            continue
    return found


def primary_engines(languages_hint: str = "rus+eng") -> list[OcrEngine]:
    """The fast tier: every installed Windows pack, then Tesseract if present."""
    found: list[OcrEngine] = windows_engines()
    for factory in (TesseractCli, PyTesseractOcr):
        try:
            engine = factory(languages_hint)
            if engine.available():
                found.append(engine)
        except Exception:
            continue
    if found:
        return found
    # No OS recogniser: the bundled model is the only thing left, and it is
    # still better than refusing to read the screenshot.
    multi = RapidOcrMulti("multi")
    if multi.available():
        found.append(multi)
    legacy = RapidOcr()
    if legacy.available():
        found.append(legacy)
    return found


def fallback_engines(skip: set[str] | None = None, limit: int = 4) -> list[OcrEngine]:
    """The slow tier: bundled multi model first, then per-script models.

    Models that are not on disk yet are included only when nothing else is left,
    because including them means a download in the middle of an index run.
    """
    skip = skip or set()
    installed = set(downloaded_models())
    found: list[OcrEngine] = []

    def add(model: str) -> None:
        if model in skip or len(found) >= limit or model not in installed:
            return
        engine = RapidOcrMulti(model)
        if engine.available():
            found.append(engine)
            skip.add(model)

    add("multi")
    covered = {languages.for_tag(tag).model for tag in windows_language_tags() if languages.for_tag(tag)}
    for model in languages.MODELS:
        if model == "multi" or model in covered:
            continue
        if model in installed:
            add(model)
    if len(found) < 2:
        # Nothing downloaded: the bundled model is all there is. The script
        # models are reachable on demand through `--ocr-lang`, not by surprise.
        legacy = RapidOcr()
        if legacy.available():
            found.append(legacy)
    return found


def script_engine(model: str) -> OcrEngine | None:
    """One named model, for `--ocr-lang korean` and for tests."""
    engine = RapidOcrMulti(model)
    return engine if engine.available() else None


def cjk_engines(rec_model: str | None = None, rec_keys: str | None = None) -> list[OcrEngine]:
    """Engines that can read Chinese, Japanese or Korean, best first."""
    found: list[OcrEngine] = []
    if sys.platform == "win32":
        for tag in CJK_TAGS:
            engine = WindowsOcr(tag)
            if engine.available():
                found.append(engine)
                break
    multi = RapidOcrMulti("multi")
    if multi.available():
        found.append(multi)
    korean = RapidOcrMulti("korean")
    if "korean" in downloaded_models() and korean.available():
        found.append(korean)
    legacy = RapidOcr(rec_model, rec_keys)
    if legacy.available():
        found.append(legacy)
    tesseract = TesseractCli("chi_sim+jpn+kor+eng")
    if tesseract.available():
        found.append(tesseract)
    return found


def coverage() -> dict:
    """What can be read on this machine, and by what."""
    models = set(downloaded_models())
    packs = windows_language_tags()
    readable = languages.greeting_pool(models)
    # greeting_pool falls back to English when nothing is installed so the boot
    # screen always has a greeting; make coverage agree with that list instead
    # of reporting a language whose model is missing.
    models.update(language.model for language in readable)
    return {
        "windows": packs,
        "models": sorted(models),
        "languages": [
            {
                "tag": language.tag,
                "name": language.name,
                "hello": language.hello,
                "model": language.model,
            }
            for language in readable
        ],
        "total": len(readable),
    }


def cjk_coverage() -> dict:
    """Which scripts CJK OCR can actually handle on this machine."""
    engines = cjk_engines()
    scripts = set()
    for engine in engines:
        if isinstance(engine, WindowsOcr):
            tag = (engine.language or "").lower()
            if tag.startswith("zh"):
                scripts.update({"chinese"})
            elif tag.startswith("ja"):
                scripts.update({"japanese", "chinese"})
            elif tag.startswith("ko"):
                scripts.update({"korean"})
        elif isinstance(engine, RapidOcrMulti):
            if engine.lang == "multi":
                scripts.update({"chinese", "japanese"})
            elif engine.lang == "korean":
                scripts.add("korean")
        elif isinstance(engine, (RapidOcr, TesseractCli)):
            scripts.update({"chinese", "japanese", "korean"})
    return {
        "engines": [
            engine.name
            + (f":{engine.language}" if isinstance(engine, WindowsOcr) else "")
            + (f":{engine.lang}" if isinstance(engine, RapidOcrMulti) else "")
            for engine in engines
        ],
        "scripts": sorted(scripts),
    }


def get_engine(preferred: str | None = None, languages_hint: str = "rus+eng") -> OcrEngine | None:
    engines = available_engines(languages_hint)
    if preferred:
        for engine in engines:
            if engine.name == preferred or getattr(engine, "label", "") == preferred:
                return engine
    return engines[0] if engines else None
