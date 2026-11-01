"""OCR engines.

The chain tries, in order: the Windows built-in recogniser (via WinRT, ships
with the OS and knows Russian if the language pack is installed), Tesseract on
PATH, pytesseract, then RapidOCR. The first available engine wins; the pipeline
records which one produced the text so a later engine change is visible instead
of silent.

On this machine the Windows engine is what actually runs: languages en-US and ru
are both present, no binary to install, ~150 ms per screenshot.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Protocol

from PIL import Image

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


class RapidOcr:
    """PP-OCR through onnxruntime.

    Its bundled recognition model is the Chinese one (`ch_PP-OCRv3_rec`), which
    reads Simplified Chinese and Latin text and is the only CJK-capable engine
    that needs no OS language pack and no Tesseract install. Japanese and Korean
    need a different recognition model; `rec_model` points at one if the user
    has fetched it, otherwise those screenshots fall back to the Windows engine.
    """

    name = "rapidocr"
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


FACTORIES = (
    (WindowsOcr, False),
    (TesseractCli, True),
    (PyTesseractOcr, True),
    (RapidOcr, False),
)


def available_engines(languages: str = "rus+eng") -> list[OcrEngine]:
    found: list[OcrEngine] = []
    for factory, takes_languages in FACTORIES:
        try:
            engine = factory(languages) if takes_languages else factory()
            if engine.available():
                found.append(engine)
        except Exception:
            continue
    return found


def cjk_engines(rec_model: str | None = None, rec_keys: str | None = None) -> list[OcrEngine]:
    """Engines that can read Chinese, Japanese or Korean, best first.

    A Windows language pack is preferred when it exists because it is free and
    already installed; RapidOCR is next because its Chinese model is bundled
    with the package; Tesseract is last because it needs `chi_sim` traineddata
    that users rarely have.
    """
    found: list[OcrEngine] = []
    if sys.platform == "win32":
        for tag in CJK_TAGS:
            engine = WindowsOcr(tag)
            if engine.available():
                found.append(engine)
                break
    rapid = RapidOcr(rec_model, rec_keys)
    if rapid.available():
        found.append(rapid)
    tesseract = TesseractCli("chi_sim+jpn+kor+eng")
    if tesseract.available():
        found.append(tesseract)
    return found


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
        elif isinstance(engine, RapidOcr):
            scripts.add("chinese")
        elif isinstance(engine, TesseractCli):
            scripts.update({"chinese", "japanese", "korean"})
    return {
        "engines": [engine.name + (f":{engine.language}" if isinstance(engine, WindowsOcr) else "")
                    for engine in engines],
        "scripts": sorted(scripts),
    }


def get_engine(preferred: str | None = None, languages: str = "rus+eng") -> OcrEngine | None:
    engines = available_engines(languages)
    if preferred:
        for engine in engines:
            if engine.name == preferred:
                return engine
    return engines[0] if engines else None
