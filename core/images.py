"""Screenshot discovery, preprocessing and visual features.

Pillow does the image work. Preprocessing exists because OCR on a dark-theme
screenshot with 11px text is where the layer usually breaks: invert dark
backgrounds, upscale small text, then hand a clean grayscale image to the
engine.
"""

from __future__ import annotations

import hashlib
import io
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:  # Pillow >= 9.1
    RESAMPLE = Image.Resampling.LANCZOS
except AttributeError:  # pragma: no cover - old Pillow
    RESAMPLE = Image.LANCZOS

THUMB_SIZE = 320
DARK_THRESHOLD = 118
SMALL_TEXT_WIDTH = 1100
MAX_PIXELS = 20_000_000


@dataclass(slots=True)
class ImageInfo:
    path: str
    width: int
    height: int
    size: int
    mtime: float
    sha256: str


def default_image_roots() -> list[Path]:
    """Standard screenshot locations for the current OS."""
    home = Path.home()
    candidates: list[Path] = []
    if os.name == "nt":
        pictures = home / "Pictures"
        candidates += [
            pictures / "Screenshots",
            pictures / "Screenshot",
            home / "OneDrive" / "Pictures" / "Screenshots",
            home / "Desktop",
            pictures,
        ]
    elif os.sys.platform == "darwin":
        candidates += [home / "Desktop", home / "Pictures"]
    else:
        candidates += [home / "Pictures", home / "Изображения", home / "Desktop"]
    return [path for path in candidates if path.is_dir()]


def load_image(path: str | Path) -> Image.Image | None:
    try:
        with Image.open(path) as handle:
            handle.load()
            return handle.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_info(path: str | Path) -> ImageInfo | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    image = load_image(path)
    if image is None:
        return None
    width, height = image.size
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return ImageInfo(str(Path(path).resolve()), width, height, stat.st_size, stat.st_mtime, digest.hexdigest())


def preprocess_for_ocr(
    image: Image.Image,
    mode: str = "rgb",
    invert_dark: bool = True,
    upscale_small: bool = True,
) -> Image.Image:
    """Normalise a screenshot for the OCR engine.

    The two engines want opposite things. Windows OCR handles colour and dark
    themes natively and measurably degrades when fed grayscale with aggressive
    contrast (136 vs 162 characters on the same screenshot), so it gets the
    image as-is plus an upscale when the text is small. Tesseract expects dark
    text on a light background and does need the inversion.
    """
    working = image.convert("RGB")
    if working.width * working.height > MAX_PIXELS:
        scale = (MAX_PIXELS / (working.width * working.height)) ** 0.5
        working = working.resize(
            (max(1, int(working.width * scale)), max(1, int(working.height * scale))), RESAMPLE
        )

    if upscale_small and working.width < SMALL_TEXT_WIDTH:
        factor = min(2.0, SMALL_TEXT_WIDTH / max(working.width, 1))
        working = working.resize((int(working.width * factor), int(working.height * factor)), RESAMPLE)

    if mode == "rgb":
        return working

    gray = working.convert("L")
    if invert_dark and float(np.asarray(gray).mean()) < DARK_THRESHOLD:
        gray = ImageOps.invert(gray)
    return ImageOps.autocontrast(gray, cutoff=1).convert("RGB")


def thumbnail_bytes(image: Image.Image, size: int = THUMB_SIZE, quality: int = 82) -> bytes:
    thumb = image.copy()
    thumb.thumbnail((size, size), RESAMPLE)
    buffer = io.BytesIO()
    thumb.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
    return buffer.getvalue()


def visual_features(image: Image.Image) -> np.ndarray:
    """Layout-and-colour descriptor: coarse RGB grid, HSV histogram, edge density.

    Not semantic, but it makes "show me screenshots that look like this one"
    work without any model weights, which is the honest baseline until CLIP is
    bundled.
    """
    working = image.convert("RGB")
    grid = working.resize((8, 8), RESAMPLE)
    grid_vector = (np.asarray(grid, dtype=np.float32) / 255.0).reshape(-1)

    hsv = np.asarray(working.convert("HSV").resize((64, 64), RESAMPLE), dtype=np.uint8)
    hue = np.histogram(hsv[..., 0], bins=12, range=(0, 255))[0].astype(np.float32)
    sat = np.histogram(hsv[..., 1], bins=4, range=(0, 255))[0].astype(np.float32)
    val = np.histogram(hsv[..., 2], bins=4, range=(0, 255))[0].astype(np.float32)
    histogram = np.concatenate([hue, sat, val])
    histogram /= max(histogram.sum(), 1.0)

    edges = _edge_grid(working)
    vector = np.concatenate([grid_vector, histogram, edges]).astype(np.float32)
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


def _edge_grid(image: Image.Image, cells: int = 4) -> np.ndarray:
    gray = np.asarray(image.convert("L").resize((64, 64), RESAMPLE), dtype=np.float32)
    gy, gx = np.gradient(gray)
    magnitude = np.hypot(gx, gy)
    cell = 64 // cells
    density = np.zeros((cells, cells), dtype=np.float32)
    for row in range(cells):
        for col in range(cells):
            block = magnitude[row * cell:(row + 1) * cell, col * cell:(col + 1) * cell]
            density[row, col] = block.mean()
    density /= max(density.max(), 1e-6)
    return density.reshape(-1)
