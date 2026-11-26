"""The Indra OCR engine (datalab-to/indra-ocr) as an accurate-tier engine.

This is a Python port of Kensaku's native port of `indra_ocr`, reduced the
same way: the page is turned upright and straightened, text lines are detected,
each line is routed to its script's recognizer and greedily CTC-decoded, and
lines are read back in reading order. Layout analysis, markdown, math and the
n-gram language model of the Python package are left out — search needs the
words, not the typography.

Why port it at all: AutoFind's OCR chain is fast (Windows packs ~10 ms, bundled
PP-OCR ~1 s) but neither is great at hard screenshots — small UI text, dense
Cyrillic, RTL. Indra ships five recognizers (base/CJK/RTL/world), a script
classifier, an orientation model and its own detector, which measurably reads
those better. It slots into the existing engine chain like the other tiers:
`available()` gates it, `recognize()` may fail and the caller falls back.

Implementation choices:

* numpy + onnxruntime only. Both are already dependencies (rapidocr brings
  numpy and opencv, onnxruntime-directml is in requirements), so the port adds
  no new package. The array math mirrors the Rust version exactly — the unit
  tests below assert the same pixel values the Rust tests assert.
* opencv is used for connected components (it is already installed for
  rapidocr); a pure-Python flood fill is the fallback.
* The model pack (about 27 MB of ONNX plus meta.json) is downloaded on demand
  and verified against pinned SHA-256s. The weights are under a modified
  OpenRAIL-M license, so they are never bundled with AutoFind.
* Sessions run CPU-only. The Rust port does the same: the models are small and
  the indexer parallelises at the image level, not inside one inference.

`AUTOFIND_INDRA_PACK_DIR` overrides the pack directory (development and the
real-model tests), like Kensaku's env var does.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# ----------------------------------------------------------------------- pack

# Files of the pack the port reads. The n-gram language models, the math and
# the reading-order models of the Python package are not needed.
PACK_FILES: tuple[str, ...] = (
    "det.onnx",
    "rec.onnx",
    "rec_cjk.onnx",
    "rec_rtl.onnx",
    "rec_world.onnx",
    "script.onnx",
    "orient.onnx",
    "meta.json",
)

_PACK_URLS = (
    "https://huggingface.co/datalab-to/indra-ocr/resolve/v1.1.0/{name}",
    # Mirror fallback: huggingface is blocked on some networks.
    "https://hf-mirror.com/datalab-to/indra-ocr/resolve/v1.1.0/{name}",
)


@dataclass(frozen=True)
class Artifact:
    """One pack file, pinned: byte length and SHA-256 catch a truncated or
    tampered download, and a mirror serves the same pinned bytes."""

    name: str
    bytes_expected: int
    sha256: str


INDRA_ARTIFACTS: tuple[Artifact, ...] = (
    Artifact("det.onnx", 389_948, "f2bd4cba23e9c328cf7ec824634e423e191a2c6aacbbf35c62bd79dd70f62f59"),
    Artifact("rec.onnx", 5_042_161, "475972a2710be4a3cbfeb2bc7f3624ffd563de07de42476fc7a290d4e1ece484"),
    Artifact("rec_cjk.onnx", 10_176_018, "4a035516a17e62cdf33a4fe2d88077654cb8841cff024685cdef4e351694ef50"),
    Artifact("rec_rtl.onnx", 4_812_710, "468796bd684486a8388c30c8b4ed2a26e3eeb1f8ff02831c24c2e12c79a2a834"),
    Artifact("rec_world.onnx", 5_627_990, "c8d1e59f21b04966f179d2d2d8e22b230d22110b3f89855bbbf2624a58c40043"),
    Artifact("script.onnx", 191_630, "b6ce4e926e19c243faaf37bd46880398bf2cb4ae56a29e1177a3b017c1280e36"),
    Artifact("orient.onnx", 371_802, "20eaf670f55a7cf9facfe1bdd6c7b8e40be75bbf4d48a3df43f66aa3bbd874fd"),
    Artifact("meta.json", 211_869, "4ce4c94b46f5d644033c02e718322014ff4081270f385038c571912b56696339"),
)


def pack_dir() -> Path:
    """Where the pack lives. `AUTOFIND_INDRA_PACK_DIR` overrides it."""
    override = os.environ.get("AUTOFIND_INDRA_PACK_DIR")
    if override:
        return Path(override)
    from ..config import default_home

    return default_home() / "ocr-indra"


def pack_present() -> bool:
    """Every pack file is on disk. Content is checked lazily by download/verify."""
    directory = pack_dir()
    return all((directory / name).is_file() for name in PACK_FILES)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_pack() -> str | None:
    """Why the pack on disk cannot be trusted yet, or `None` when it can.

    Doctor and the engine report use this instead of `pack_present()`: a
    half-downloaded pack is present and useless.
    """
    directory = pack_dir()
    for artifact in INDRA_ARTIFACTS:
        path = directory / artifact.name
        if not path.is_file():
            return f"missing {artifact.name}"
        if path.stat().st_size != artifact.bytes_expected:
            return f"{artifact.name}: {path.stat().st_size} bytes, expected {artifact.bytes_expected}"
        if file_sha256(path) != artifact.sha256:
            return f"{artifact.name}: checksum mismatch"
    return None


def download_pack(progress=None) -> Path:
    """Download and verify every pack file. Returns the pack directory.

    An existing file with the right checksum is kept, so an interrupted run
    resumes. Each file downloads to a `.part` temp file and is renamed in
    place after verification — a killed download never leaves a plausible
    looking half-model behind.
    """
    directory = pack_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for artifact in INDRA_ARTIFACTS:
        path = directory / artifact.name
        if path.is_file() and path.stat().st_size == artifact.bytes_expected:
            if file_sha256(path) == artifact.sha256:
                continue
            path.unlink()  # wrong bytes on disk: redownload
        errors: list[str] = []
        for url in _PACK_URLS:
            target = url.format(name=artifact.name)
            try:
                if progress:
                    progress(artifact.name, 0, artifact.bytes_expected)
                tmp = path.with_name(path.name + ".part")
                with urllib.request.urlopen(target, timeout=120) as response, open(tmp, "wb") as out:
                    total = 0
                    digest = hashlib.sha256()
                    while True:
                        block = response.read(1 << 20)
                        if not block:
                            break
                        out.write(block)
                        digest.update(block)
                        total += len(block)
                        if progress:
                            progress(artifact.name, total, artifact.bytes_expected)
                if total != artifact.bytes_expected or digest.hexdigest() != artifact.sha256:
                    raise RuntimeError(f"checksum mismatch after download ({total} bytes)")
                tmp.replace(path)
                break
            except Exception as exc:  # noqa: BLE001 - try the next mirror
                errors.append(f"{url.split('/')[2]}: {exc}")
                try:
                    path.with_name(path.name + ".part").unlink()
                except OSError:
                    pass
        else:
            raise RuntimeError(f"cannot download {artifact.name}: " + "; ".join(errors))
    return directory


def indra_status() -> dict:
    """Engine report entry: what a user can act on."""
    directory = pack_dir()
    if not pack_present():
        return {
            "available": False,
            "pack_dir": str(directory),
            "problem": "models are not downloaded yet (autofind ocr download indra)",
        }
    problem = verify_pack()
    if problem:
        return {"available": False, "pack_dir": str(directory), "problem": problem}
    return {"available": True, "pack_dir": str(directory), "problem": None}


# ------------------------------------------------------------------- imageops
# 8-bit grayscale page, row-major, [h, w] (0 = black, 255 = white). The Rust
# port wraps a Vec<u8> in a Gray struct; numpy arrays are that struct.


def gray_from_image(image) -> np.ndarray:
    """Grayscale with OpenCV's BT.601 weights, transparency composited over
    white (a transparent screenshot would otherwise read as a black page)."""
    rgba = np.asarray(image.convert("RGBA"), dtype=np.uint32)
    alpha = rgba[..., 3]
    over = lambda channel: (channel * alpha + 255 * (255 - alpha) + 127) // 255
    red = over(rgba[..., 0])
    green = over(rgba[..., 1])
    blue = over(rgba[..., 2])
    return ((red * 4899 + green * 9617 + blue * 1868 + 8192) >> 14).astype(np.uint8)


def _area_taps(src: int, dst: int) -> np.ndarray:
    """OpenCV INTER_AREA for shrinking, as a [dst, src] matrix: each output
    pixel averages the input span it covers, partial pixels weighted."""
    scale = src / dst
    matrix = np.zeros((dst, src), dtype=np.float32)
    for i in range(dst):
        start, end = i * scale, (i + 1) * scale
        j = int(start)
        while j < end and j < src:
            cover = min(end, j + 1.0) - max(start, float(j))
            if cover > 1e-9:
                matrix[i, j] = cover / scale
            j += 1
    return matrix


def _linear_taps(src: int, dst: int) -> np.ndarray:
    """OpenCV INTER_LINEAR as a [dst, src] matrix: half-pixel centres, clamped
    edges. When both taps hit the same pixel their weights sum to one."""
    scale = src / dst
    matrix = np.zeros((dst, src), dtype=np.float32)
    for i in range(dst):
        fx = max((i + 0.5) * scale - 0.5, 0.0)
        j0 = min(int(fx), src - 1)
        j1 = min(j0 + 1, src - 1)
        t = min(max(fx - j0, 0.0), 1.0)
        matrix[i, j0] += 1.0 - t
        matrix[i, j1] += t
    return matrix


def resize(gray: np.ndarray, w: int, h: int) -> np.ndarray:
    """Resizes to w x h: area averaging when shrinking, bilinear otherwise
    (the choice every resize in `indra_ocr` makes). Separable, two matmuls."""
    w, h = max(w, 1), max(h, 1)
    src_h, src_w = gray.shape
    if (w, h) == (src_w, src_h):
        return gray.copy()
    shrink = h < src_h  # one flag for both axes, like the Rust port
    taps = _area_taps if shrink else _linear_taps
    tmp = gray.astype(np.float32) @ taps(src_w, w).T
    out = taps(src_h, h) @ tmp
    # np.rint rounds half to even, the Rust f64::round half away from zero; a
    # half-pixel value is only reachable from a 50/50 area mix, and one gray
    # level of drift is invisible to the detector.
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _scaled(n: int, s: float) -> int:
    return max(round(n * s), 1)


def preview(gray: np.ndarray, side: int) -> np.ndarray:
    """The page shrunk to at most `side` pixels a side."""
    s = side / max(gray.shape[1], gray.shape[0])
    if s >= 1.0:
        return gray.copy()
    return resize(gray, _scaled(gray.shape[1], s), _scaled(gray.shape[0], s))


def _ink_padded(small: np.ndarray, pw: int, ph: int) -> np.ndarray:
    """Ink map (1 = ink) padded with paper to pw x ph."""
    out = np.zeros((ph, pw), dtype=np.float32)
    h, w = min(small.shape[0], ph), min(small.shape[1], pw)
    out[:h, :w] = 1.0 - small[:h, :w] / 255.0
    return out


def page_to_input(gray: np.ndarray, long_side: int) -> tuple[np.ndarray, int, int, float]:
    """Detector input: long side scaled to `long_side`, padded to a multiple
    of 32. Returns (ink, height, width, scale)."""
    scale = long_side / max(gray.shape[1], gray.shape[0])
    small = resize(gray, _scaled(gray.shape[1], scale), _scaled(gray.shape[0], scale))
    ph = -(-small.shape[0] // 32) * 32
    pw = -(-small.shape[1] // 32) * 32
    return _ink_padded(small, pw, ph), ph, pw, scale


def orient_input(gray: np.ndarray, side: int) -> np.ndarray:
    """Orientation model input: long side `side`, padded square."""
    s = side / max(gray.shape[1], gray.shape[0])
    small = resize(gray, _scaled(gray.shape[1], s), _scaled(gray.shape[0], s))
    return _ink_padded(small, side, side)


@dataclass(slots=True)
class LineBox:
    """A detected text line in page pixels."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def height(self) -> float:
        return self.y1 - self.y0


OUT_STRIDE = 2.0
CORE_X_SHRINK = 0.15
# The detector's core channel is a probability, not an unbounded logit. At 0.0
# noise pixels become "lines", count explodes and a blank page reads as CJK.
# Keep this in step with the documented `p > 0.5` contract in decode_lines.
CORE_THRESHOLD = 0.5


def _label_components(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """4-neighbour connected-component labelling of a boolean mask.

    Labels are 1..count, 0 is background — the same contract
    `scipy.ndimage.label` used to serve. OpenCV is the fast path (it is already
    a dependency through rapidocr); a bounded pure-Python flood fill stands in
    when cv2 is missing, so the engine degrades slowly instead of failing on
    every page.
    """
    try:
        import cv2

        count, labels = cv2.connectedComponents(
            mask.astype(np.uint8), connectivity=4
        )
        return labels, int(count) - 1  # cv2 labels the background 0
    except ImportError:
        pass
    labels = np.zeros(mask.shape, dtype=np.int32)
    count = 0
    height, width = mask.shape
    for start_row in range(height):
        row = mask[start_row]
        for start_col in np.flatnonzero(row):
            start_col = int(start_col)
            if labels[start_row, start_col]:
                continue
            count += 1
            stack = [(start_row, start_col)]
            labels[start_row, start_col] = count
            while stack:
                r, c = stack.pop()
                if r > 0 and mask[r - 1, c] and not labels[r - 1, c]:
                    labels[r - 1, c] = count
                    stack.append((r - 1, c))
                if r + 1 < height and mask[r + 1, c] and not labels[r + 1, c]:
                    labels[r + 1, c] = count
                    stack.append((r + 1, c))
                if c > 0 and mask[r, c - 1] and not labels[r, c - 1]:
                    labels[r, c - 1] = count
                    stack.append((r, c - 1))
                if c + 1 < width and mask[r, c + 1] and not labels[r, c + 1]:
                    labels[r, c + 1] = count
                    stack.append((r, c + 1))
    return labels, count


def decode_lines(out: np.ndarray, h: int, w: int) -> list[LineBox]:
    """Decodes the detector output: `out` is [C, h, w] with the core logit,
    distance to the line top and distance to the line bottom in channels 0-2.
    `p > 0.5` marks core pixels; each connected component (4-neighbour, like
    the Rust flood fill) becomes one line whose top/bottom is the ink-weighted
    average of the per-pixel distances. Boxes are in detector input pixels."""
    if out.ndim == 4:  # onnxruntime keeps the batch dimension
        out = out[0]
    if out.ndim != 3 or out.shape[0] < 3:
        return []
    mask = out[0] > CORE_THRESHOLD
    labels, count = _label_components(mask)
    if count == 0:
        return []
    owned = labels.ravel() > 0
    lab = labels.ravel()[owned] - 1
    count_per = np.bincount(lab, minlength=count)
    rows = np.repeat(np.arange(h, dtype=np.float32), w)[owned]
    cols = np.tile(np.arange(w, dtype=np.float32), h)[owned]
    cy = (rows + 0.5) * OUT_STRIDE
    top_sum = np.bincount(lab, weights=cy - out[1].ravel()[owned], minlength=count)
    bot_sum = np.bincount(lab, weights=cy + out[2].ravel()[owned], minlength=count)
    left = np.full(count, w, dtype=np.int64)
    right = np.full(count, -1, dtype=np.int64)
    cols_i = cols.astype(np.int64)
    np.minimum.at(left, lab, cols_i)
    np.maximum.at(right, lab, cols_i)
    boxes: list[LineBox] = []
    for i in range(count):
        if count_per[i] < 2:  # a lone pixel is noise, not a line
            continue
        y0, y1 = float(top_sum[i] / count_per[i]), float(bot_sum[i] / count_per[i])
        ex = CORE_X_SHRINK * max(y1 - y0, 1.0)
        boxes.append(
            LineBox(
                x0=left[i] * OUT_STRIDE - ex,
                y0=y0,
                x1=(right[i] + 1) * OUT_STRIDE + ex,
                y1=y1,
            )
        )
    return boxes


def is_dark_line(gray: np.ndarray, box: LineBox) -> bool:
    """Light text on a dark ground: the ink profile is skewed to the dark end
    (98th percentile far above the median), so the line is read inverted."""
    x0, y0 = max(int(box.x0), 0), max(int(box.y0), 0)
    x1, y1 = min(int(box.x1) + 1, gray.shape[1]), min(int(box.y1) + 1, gray.shape[0])
    if x0 >= x1 or y0 >= y1:
        return False
    region = gray[y0:y1, x0:x1].ravel()
    lo, med, hi = np.percentile(region, [2, 50, 98])
    return float(hi - med) > 2.0 * float(med - lo) + 8.0


PAD_Y = 0.2
PAD_X = 0.3
WIDTH_MULTIPLE = 8


@dataclass(slots=True)
class Crop:
    """Recognizer input of one line: an ink map `height * window` px tall."""

    h: int
    w: int
    ink: np.ndarray


def crop_line(
    gray: np.ndarray, box: LineBox, height: int, window: int, dark: bool
) -> Crop:
    """One line cropped and scaled. A tall window (window > 1) keeps the text
    scale and adds the page above and below."""
    pad_y = (window * (1.0 + 2.0 * PAD_Y) - 1.0) / 2.0
    height = height * window
    line_h = max(box.height, 1.0)
    ix0 = int(math.floor(box.x0 - PAD_X * line_h))
    iy0 = int(math.floor(box.y0 - pad_y * line_h))
    ix1 = int(math.ceil(box.x1 + PAD_X * line_h))
    iy1 = int(math.ceil(box.y1 + pad_y * line_h))
    cw, ch = max(ix1 - ix0, 1), max(iy1 - iy0, 1)
    crop = np.full((ch, cw), 0 if dark else 255, dtype=np.uint8)
    sy0, sy1 = max(iy0, 0), min(iy1, gray.shape[0])
    sx0, sx1 = max(ix0, 0), min(ix1, gray.shape[1])
    if sy0 < sy1 and sx0 < sx1:
        crop[sy0 - iy0 : sy1 - iy0, sx0 - ix0 : sx1 - ix0] = gray[sy0:sy1, sx0:sx1]
    out_w = int(round(cw * height / ch))
    out_w = min(max(out_w, WIDTH_MULTIPLE), 4096)
    resized = resize(crop, out_w, height)
    ink = resized.astype(np.float32) / 255.0
    if not dark:
        ink = 1.0 - ink
    return Crop(h=height, w=out_w, ink=np.ascontiguousarray(ink))


def pad_batch(crops: list[Crop]) -> np.ndarray:
    """Right-pads crops of one height to a common width: [B, 1, H, W]."""
    h = crops[0].h
    wm = -(-max(c.w for c in crops) // WIDTH_MULTIPLE) * WIDTH_MULTIPLE
    out = np.zeros((len(crops), 1, h, wm), dtype=np.float32)
    for k, crop in enumerate(crops):
        out[k, 0, :, : crop.w] = crop.ink
    return out


def width_batches(widths: list[int]) -> list[list[int]]:
    """Groups line indices into batches of similar width, so little compute
    goes to padding and no batch grows past 64 x 640 columns."""
    MAX_BATCH, MAX_RATIO, MIN_WIDTH, MAX_AREA = 64, 1.3, 64, 64 * 640
    order = sorted(range(len(widths)), key=lambda i: widths[i])
    batches: list[list[int]] = []
    for i in order:
        w = max(widths[i], MIN_WIDTH)
        if batches:
            head = max(widths[batches[-1][0]], MIN_WIDTH)
            fits = (
                len(batches[-1]) < MAX_BATCH
                and w <= MAX_RATIO * head
                and (len(batches[-1]) + 1) * w <= MAX_AREA
            )
        else:
            fits = False
        if fits:
            batches[-1].append(i)
        else:
            batches.append([i])
    return batches


# ----------------------------------------------------------------------- text
# A recognizer's token list: class 0 is the CTC blank, class i + 1 is
# chars[i] (a character, or a LaTeX command in the base vocabulary).


class Vocab:
    """One recognizer's alphabet, parsed from a meta.json `vocab*` entry:
    a JSON string holding {"blank": 0, "chars": [...]}."""

    __slots__ = ("chars", "has_commands", "jamo")

    def __init__(self, entry) -> None:
        if isinstance(entry, str):
            entry = json.loads(entry)
        if not isinstance(entry, dict) or int(entry.get("blank", 0) or 0) != 0:
            raise ValueError("vocab: only blank = 0 is supported")
        chars = entry.get("chars")
        if not isinstance(chars, list):
            raise ValueError("vocab: no chars")
        self.chars = [str(item) for item in chars]
        self.has_commands = any(len(token) > 1 for token in self.chars)
        # The CJK recognizer reads Korean as conjoining jamo; their presence
        # is detected by the syllable-block starter L (U+1100).
        self.jamo = any(token == chr(0x1100) for token in self.chars)

    def token(self, id_: int) -> str | None:
        index = id_ - 1
        if 0 <= index < len(self.chars):
            return self.chars[index]
        return None

    def decode(self, ids) -> str:
        """Text of the base recognizer's token ids."""
        tokens = [token for token in (self.token(i) for i in ids) if token]
        if self.jamo:
            return compose_hangul("".join(tokens))
        out: list[str] = []
        for k, token in enumerate(tokens):
            out.append(token)
            # "\\alpha" + "x" must not read back as "\\alphax".
            command = (
                self.has_commands
                and len(token) > 2
                and token.startswith("\\")
                and token[1:].isalpha()
            )
            nxt = tokens[k + 1] if k + 1 < len(tokens) else ""
            if command and nxt[:1].isalpha():
                out.append(" ")
        return "".join(out)

    def decode_chars(self, ids) -> str:
        """Text of a script-group recognizer's tokens: whitespace collapsed,
        jamo composed. Right-to-left lines are still in visual order."""
        raw = "".join(token for token in (self.token(i) for i in ids) if token)
        return compose_hangul(" ".join(raw.split()))


def compose_hangul(text: str) -> str:
    """NFC for Hangul only: conjoining jamo (L V [T]) become syllables. Search
    needs the composed syllables, not the jamo the recognizer emits."""
    S_BASE, L_BASE, V_BASE, T_BASE = 0xAC00, 0x1100, 0x1161, 0x11A7
    L_COUNT, V_COUNT, T_COUNT = 19, 21, 28
    out: list[str] = []
    for ch in text:
        code = ord(ch)
        if out:
            prev = ord(out[-1])
            if L_BASE <= prev < L_BASE + L_COUNT and V_BASE <= code < V_BASE + V_COUNT:
                out[-1] = chr(S_BASE + ((prev - L_BASE) * V_COUNT + (code - V_BASE)) * T_COUNT)
                continue
            lv = prev - S_BASE
            if (
                0 <= lv < L_COUNT * V_COUNT * T_COUNT
                and lv % T_COUNT == 0
                and T_BASE + 1 <= code < T_BASE + T_COUNT
            ):
                out[-1] = chr(prev + code - T_BASE)
                continue
        out.append(ch)
    return "".join(out)


# Combining marks of the right-to-left scripts and generic diacritics: they
# stay after their base character when a run is reversed.
_MARK_RANGES = (
    (0x0300, 0x036F), (0x0591, 0x05BD), (0x05BF, 0x05BF), (0x05C1, 0x05C2),
    (0x05C4, 0x05C5), (0x05C7, 0x05C7), (0x0610, 0x061A), (0x064B, 0x065F),
    (0x0670, 0x0670), (0x06D6, 0x06DC), (0x06DF, 0x06E4), (0x06E7, 0x06E8),
    (0x06EA, 0x06ED),
)


def _is_mark(ch: str) -> bool:
    return any(lo <= ord(ch) <= hi for lo, hi in _MARK_RANGES)


def _is_arabic_digit(ch: str) -> bool:
    return ord(ch) in range(0x0660, 0x066A) or ord(ch) in range(0x06F0, 0x06FA)


def is_rtl(ch: str) -> bool:
    """Bidi class R or AL (Hebrew, Arabic, Syriac, Thaana, N'Ko and their
    presentation forms), without marks and digits."""
    code = ord(ch)
    if not (
        0x0590 <= code <= 0x08FF or 0xFB1D <= code <= 0xFDFF or 0xFE70 <= code <= 0xFEFF
    ):
        return False
    return not _is_mark(ch) and not _is_arabic_digit(ch) and not ch.isspace()


def _is_ltr(ch: str) -> bool:
    """Bidi class L, EN or AN: reads left to right inside right-to-left text."""
    return not is_rtl(ch) and not _is_mark(ch) and ch.isalnum()


def flip(text: str, base_rtl: bool) -> str:
    """Logical order from visual order (the operation is its own inverse).
    `base_rtl`: the page reads right to left."""
    if not any(is_rtl(ch) for ch in text):
        return text
    clusters: list[str] = []
    for ch in text:
        if clusters and _is_mark(ch):
            clusters[-1] += ch
        else:
            clusters.append(ch)

    def kind(cluster: str) -> str:
        first = cluster[:1] or " "
        if is_rtl(first):
            return "R"
        if _is_ltr(first):
            return "L"
        return "N"

    kinds = [kind(cluster) for cluster in clusters]

    # [start, end) spans from a `strong` cluster to the last `strong` one
    # before a `stop` cluster.
    def runs(strong: str, stop: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        start: int | None = None
        last = 0
        for i in list(range(len(kinds))) + [len(kinds)]:
            k = kinds[i] if i < len(kinds) else "#"
            if k == strong:
                start = start if start is not None else i
                last = i
            elif (k == stop or k == "#") and start is not None:
                spans.append((start, last + 1))
                start = None
        return spans

    def mirror(cluster: str) -> str:
        pairs = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{",
                 "<": ">", ">": "<", "«": "»", "»": "«"}
        head, tail = cluster[:1], cluster[1:]
        return pairs.get(head, head) + tail

    if base_rtl:
        # Units: left-to-right runs kept whole, every other cluster alone;
        # the units reverse.
        units: list[str] = []
        i = 0
        for a, b in runs("L", "R") + [(len(clusters), len(clusters))]:
            units.extend(mirror(cluster) for cluster in clusters[i:a])
            if a < b:
                units.append("".join(clusters[a:b]))
            i = b
        return "".join(reversed(units))
    # Left-to-right base: each right-to-left run reverses in place.
    out = list(clusters)
    for a, b in runs("R", "L"):
        for k, cluster in enumerate(reversed(clusters[a:b])):
            out[a + k] = mirror(cluster)
    return "".join(out)


def page_is_rtl(texts) -> bool:
    """Whether most letters on the page are right-to-left."""
    rtl = letters = 0
    for text in texts:
        for ch in text:
            if ch.isalpha():
                letters += 1
                if is_rtl(ch):
                    rtl += 1
    return rtl > letters - rtl


def is_leader_line(text: str, width: float, height: float) -> bool:
    """A line of leader characters only (dot leaders, fill-in rules, specks)
    carries no text. A short lone dash stays: it can be an empty table cell."""
    leaders = ".·…‥_-–—="
    core = [ch for ch in text if ch != " "]
    if not core or not all(ch in leaders for ch in core):
        return False
    return (
        len(core) >= 3
        or width >= 3.0 * height
        or not any(ch in "-–—" for ch in core)
    )


# ---------------------------------------------------------------------- order
# Reading order by recursive XY-cut: split at the widest whitespace gap on
# either axis, upper or left part first.


def reading_order(boxes: list[LineBox]) -> list[int]:
    def best_cut(boxes: list[LineBox], ids: list[int], rows: bool):
        def span(i: int):
            box = boxes[i]
            return (box.y0, box.y1) if rows else (box.x0, box.x1)

        order = sorted(ids, key=span)
        end = span(order[0])[1]
        gap, at = 0.0, 0
        for k, i in enumerate(order[1:], start=1):
            g = span(i)[0] - end
            if g > gap:
                gap, at = g, k
            end = max(end, span(i)[1])
        return gap, at, order

    def top_down(boxes: list[LineBox], ids: list[int]) -> list[int]:
        heights = sorted(boxes[i].height for i in ids)
        half = max(heights[len(heights) // 2] * 0.5, 1e-3)

        def row(i: int) -> float:
            box = boxes[i]
            return round((box.y0 + box.y1) / 2.0 / half)

        return sorted(ids, key=lambda i: (row(i), boxes[i].x0))

    def recurse(boxes: list[LineBox], ids: list[int], out: list[int]) -> None:
        if len(ids) <= 1:
            out.extend(ids)
            return
        gy, ky, oy = best_cut(boxes, ids, True)
        gx, kx, ox = best_cut(boxes, ids, False)
        if ky == 0 and kx == 0:
            out.extend(top_down(boxes, ids))
            return
        k, order = (ky, oy) if gy >= gx else (kx, ox)
        recurse(boxes, order[:k], out)
        recurse(boxes, order[k:], out)

    out: list[int] = []
    recurse(boxes, list(range(len(boxes))), out)
    return out


# ----------------------------------------------------------------------- skew
# Page tilt: the angle whose horizontal ink profile changes most sharply row
# to row is the one that levels the text rows.

# Degrees of tilt searched (quarter turns cover the rest).
DESKEW_MAX = 45.0
# Smaller tilts are left alone (the search step is 0.1).
DESKEW_MIN = 0.3


def rotate(gray: np.ndarray, degrees: float, replicate: bool) -> np.ndarray:
    """Rotates by `degrees` counter-clockwise about the centre, same size,
    bilinear sampling. `replicate`: edge pixels fill the corners (a page);
    otherwise 0 (an ink map)."""
    radians = math.radians(degrees)
    sin, cos = math.sin(radians), math.cos(radians)
    h, w = gray.shape
    cx, cy = w / 2.0, h / 2.0
    ys, xs = np.mgrid[0:h, 0:w]
    dx = xs.astype(np.float32) - cx
    dy = ys.astype(np.float32) - cy
    sx = cos * dx - sin * dy + cx
    sy = sin * dx + cos * dy + cy
    x0 = np.floor(sx)
    y0 = np.floor(sy)
    fx = (sx - x0).astype(np.float32)
    fy = (sy - y0).astype(np.float32)
    x0 = x0.astype(np.int64)
    y0 = y0.astype(np.int64)

    def sample(x: np.ndarray, y: np.ndarray) -> np.ndarray:
        if replicate:
            return gray[np.clip(y, 0, h - 1), np.clip(x, 0, w - 1)].astype(np.float32)
        inside = (x >= 0) & (x <= w - 1) & (y >= 0) & (y <= h - 1)
        vals = np.zeros(x.shape, dtype=np.float32)
        xc, yc = np.clip(x, 0, w - 1), np.clip(y, 0, h - 1)
        vals[inside] = gray[yc[inside], xc[inside]]
        return vals

    top = sample(x0, y0) * (1.0 - fx) + sample(x0 + 1, y0) * fx
    bottom = sample(x0, y0 + 1) * (1.0 - fx) + sample(x0 + 1, y0 + 1) * fx
    out = top * (1.0 - fy) + bottom * fy
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def _ink_thumb(gray: np.ndarray, side: int) -> np.ndarray:
    """Ink map (255 = ink) of the page shrunk to `side` pixels."""
    s = side / max(gray.shape[1], gray.shape[0])
    small = resize(gray, _scaled(gray.shape[1], s), _scaled(gray.shape[0], s))
    return 255 - small


def _row_sharpness(ink: np.ndarray, degrees: float) -> float:
    turned = rotate(ink, degrees, False)
    rows = turned.astype(np.float64).sum(axis=1)
    diff = np.diff(rows)
    return float((diff * diff).sum())


def _best(ink: np.ndarray, angles: list[float]) -> float:
    """Best angle (the first one on ties, like Rust's `max`)."""
    scores = [_row_sharpness(ink, a) for a in angles]
    at = 0
    for i, score in enumerate(scores):
        if score > scores[at]:
            at = i
    return angles[at]


def _steps(start: float, end: float, step: float) -> list[float]:
    n = int(math.floor((end - start) / step + 1e-6))
    return [start + i * step for i in range(n + 1)]


def estimate_skew(gray: np.ndarray) -> float:
    """Degrees (counter-clockwise) that straighten the text rows: a 2 degree
    grid on a half-size thumbnail, then the two best angles and 0 refined in
    0.5 and 0.1 degree steps on an 800 px thumbnail."""
    SIDE = 800
    fine = _ink_thumb(gray, SIDE)
    if int(fine.sum()) < 0.003 * 255.0 * fine.size:
        return 0.0  # a blank page has no rows to straighten
    coarse = _ink_thumb(gray, SIDE // 2)
    grid = _steps(-DESKEW_MAX, DESKEW_MAX, 2.0)
    scores = [_row_sharpness(coarse, a) for a in grid]
    ranked = sorted(range(len(grid)), key=lambda i: scores[i])
    starts = [0.0, grid[ranked[-1]], grid[ranked[-2]]]
    near: list[float] = []
    for a0 in starts:
        for a in _steps(a0 - 2.0, a0 + 2.0, 0.5):
            if not any(abs(n - a) < 1e-9 for n in near):
                near.append(a)
    a1 = _best(fine, near)
    return _best(fine, _steps(a1 - 0.5, a1 + 0.5, 0.1))



# --------------------------------------------------------------------- engine
# The Indra pipeline on ONNX Runtime. Constants carried over from the Rust
# port unchanged — they were measured against the real models.

# Second detection pass when lines are this small (input pixels), scaled so
# the median line becomes ADAPTIVE_TARGET_H.
ADAPTIVE_MIN_H = 9.0
ADAPTIVE_TARGET_H = 12.0
ADAPTIVE_MAX_SIDE = 2048
PREVIEW_SIDE = 1600
ORIENT_SIDE = 768
# A turn is applied only above this probability: a wrong turn loses the page.
ORIENT_MIN = 0.95
# orient.onnx class: the page is set in vertical columns (Chinese, Japanese).
VERTICAL = 4
# A line under this mean CTC peak probability gets no text (rules, checkboxes
# and empty fields read as "-", "I", "1").
MIN_CONF = 0.5
# The script classifier routes a line to one recognizer above this
# probability; below it the base recognizer reads it too and the more
# confident reading wins.
ROUTE_SURE = 0.95


@dataclass(slots=True)
class Group:
    """One script recognizer plus its vocabulary."""

    name: str
    session: object
    vocab: Vocab


@dataclass(slots=True)
class _Spec:
    """What meta.json says, before any model is loaded."""

    script_groups: list[str]
    vocabs: dict[str, Vocab]
    long_side: int
    height: int
    window: int

    @classmethod
    def parse(cls, meta: dict) -> "_Spec":
        script_groups = list(meta.get("script_groups") or ["base", "cjk", "rtl", "other"])
        vocabs: dict[str, Vocab] = {}
        for name in script_groups:
            entry = meta.get(f"vocab_{name}")
            if entry is not None:
                vocabs[name] = Vocab(entry)
        if meta.get("vocab") is not None:
            vocabs[""] = Vocab(meta["vocab"])
        num = lambda key, default: int(meta.get(key) or default)  # noqa: E731
        return cls(
            script_groups=script_groups,
            vocabs=vocabs,
            long_side=num("det_long_side", 1024),
            height=num("rec_height", 32),
            window=max(num("rec_window", 1), 1),
        )


def _session(path: Path, threads: int):
    """One CPU session. Line batches change shape on every call, so the arena
    stays off (the Rust port disables the arena allocator for the same reason)."""
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.enable_cpu_mem_arena = False
    options.log_severity_level = 3
    return ort.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )


def _run(session, feed: dict) -> dict:
    """Output name -> array, whatever order the graph exports them in."""
    names = [output.name for output in session.get_outputs()]
    return dict(zip(names, session.run(None, feed)))


class Engine:
    """The loaded pipeline. Sessions are injected, so tests can drive the
    whole `read()` path without the 27 MB pack."""

    def __init__(
        self,
        det,
        rec,
        vocab: Vocab,
        groups: list[Group],
        script_session,
        orient,
        long_side: int,
        height: int,
        window: int,
        script_groups: list[str] | None = None,
    ) -> None:
        self.det = det
        self.rec = rec
        self.vocab = vocab
        self.groups = groups
        self.script = script_session
        self.orient = orient
        self.long_side = long_side
        self.height = height
        self.window = window
        self.script_groups = script_groups or []

    @classmethod
    def load(cls, directory: Path, threads: int = 2) -> "Engine":
        spec = _Spec.parse(json.loads((directory / "meta.json").read_text(encoding="utf-8")))
        # A pack without the bare "vocab" entry used to raise a bare KeyError here,
        # which _core() cached as "indra permanently unavailable" with no clue why.
        base = spec.vocabs.get("")
        if base is None:
            raise ValueError("indra pack meta.json is missing the base 'vocab' entry")
        groups = [
            Group(name=name, session=_session(directory / f"rec_{name}.onnx", threads), vocab=vocab)
            for name, vocab in spec.vocabs.items()
            if name and (directory / f"rec_{name}.onnx").is_file()
        ]
        optional = lambda name: (  # noqa: E731
            _session(directory / name, threads) if (directory / name).is_file() else None
        )
        return cls(
            det=_session(directory / "det.onnx", threads),
            rec=_session(directory / "rec.onnx", threads),
            vocab=base,
            groups=groups,
            script_session=optional("script.onnx") if groups else None,
            orient=optional("orient.onnx"),
            long_side=spec.long_side,
            height=spec.height,
            window=spec.window,
            script_groups=spec.script_groups,
        )

    def read(self, page: np.ndarray) -> str:
        """The page's text, one detected line per line, in reading order."""
        if page.shape[0] < 2 or page.shape[1] < 2:
            return ""
        # The orientation model reads the preview straightened by its row
        # angle (a tilted page could read as turned).
        small = preview(page, PREVIEW_SIDE)
        skew = estimate_skew(small)
        turn = (
            self.orientation(rotate(small, skew, True))
            if abs(skew) >= DESKEW_MIN
            else self.orientation(small)
        )

        def quarter(image: np.ndarray) -> np.ndarray:
            if turn == VERTICAL:
                return np.rot90(image, 1)
            return np.rot90(image, -turn)

        upright = quarter(page)
        # After a quarter turn the rows were columns: measure again.
        if turn % 2 == 1 or turn == VERTICAL:
            skew = estimate_skew(quarter(small))
        if abs(skew) >= DESKEW_MIN:
            upright = rotate(upright, skew, True)
        boxes = self.detect(upright)
        if not boxes:
            return ""
        readings = self.recognize(upright, boxes, turn == VERTICAL)
        rtl = page_is_rtl(text for text, _ in readings)
        kept: list[LineBox] = []
        texts: list[str] = []
        for box, (text, _) in zip(boxes, readings):
            text = flip(text, rtl)
            if text.strip() and not is_leader_line(text, box.x1 - box.x0, box.height):
                kept.append(box)
                texts.append(text)
        # A right-to-left page reads its right column first: order mirrored.
        if rtl:
            w = float(upright.shape[1])
            for box in kept:
                box.x0, box.x1 = w - box.x1, w - box.x0
        return "\n".join(texts[i] for i in reading_order(kept))

    def orientation(self, page: np.ndarray) -> int:
        """Quarter turns clockwise that make the page upright, or VERTICAL,
        from the page preview."""
        if self.orient is None:
            return 0
        x = orient_input(page, ORIENT_SIDE)
        out = _run(self.orient, {"x": x.reshape(1, 1, ORIENT_SIDE, ORIENT_SIDE)})
        p = next(iter(out.values()))
        k = int(np.argmax(p[0] if p.ndim > 1 else p))
        best = float(np.max(p[0] if p.ndim > 1 else p))
        return k if best > ORIENT_MIN else 0

    def _detect_at(self, page: np.ndarray, long_side: int):
        x, h, w, scale = page_to_input(page, long_side)
        out = _run(self.det, {"x": x.reshape(1, 1, h, w)})
        logits = next(iter(out.values()))
        return decode_lines(logits, *logits.shape[-2:]), scale

    def detect(self, page: np.ndarray) -> list[LineBox]:
        boxes, scale = self._detect_at(page, self.long_side)
        if len(boxes) >= 8:
            heights = sorted(box.height for box in boxes)
            median = heights[len(heights) // 2]
            if median < ADAPTIVE_MIN_H:
                side = int(self.long_side * ADAPTIVE_TARGET_H / max(median, 1.0))
                side = min(side, ADAPTIVE_MAX_SIDE) // 32 * 32
                if side > self.long_side:
                    boxes, scale = self._detect_at(page, side)
        h, w = float(page.shape[0]), float(page.shape[1])
        s = float(scale)
        for box in boxes:
            box.x0 = min(max(box.x0 / s, 0.0), w)
            box.x1 = min(max(box.x1 / s, 0.0), w)
            box.y0 = min(max(box.y0 / s, 0.0), h)
            box.y1 = min(max(box.y1 / s, 0.0), h)
        return boxes

    def recognize(self, page: np.ndarray, boxes: list[LineBox], vertical: bool):
        """Text and confidence per line box."""
        dark = [is_dark_line(page, box) for box in boxes]
        crops = [crop_line(page, box, self.height, 1, d) for box, d in zip(boxes, dark)]
        tall = (
            [crop_line(page, box, self.height, self.window, d) for box, d in zip(boxes, dark)]
            if self.window > 1
            else []
        )
        all_lines = list(range(len(crops)))
        # (group index or None for base, line indices)
        plan: list[tuple[int | None, list[int]]] = []
        cjk = next((g for g, group in enumerate(self.groups) if group.name == "cjk"), None)
        if vertical and cjk is not None:
            # A vertical page's lines are CJK columns turned on their side.
            plan.append((cjk, all_lines))
        elif self.script is not None:
            names, sure = self.line_groups(crops)
            base = [
                j
                for j in all_lines
                if names[j] is None or not sure[j]
            ]
            plan.append((None, base))
            for g in range(len(self.groups)):
                plan.append((g, [j for j in all_lines if names[j] == g]))
        else:
            plan.append((None, all_lines))

        best: list[tuple[str, float]] = [("", -1.0)] * len(crops)
        for group, lines in plan:
            src = tall if group is None and self.window > 1 else crops
            # Batch on the crops actually fed to the recognizer: a tall window
            # changes the width, so crops[j].w understates it and can push a
            # batch past MAX_AREA.
            widths = [src[j].w for j in lines]
            for part in width_batches(widths):
                idx = [lines[i] for i in part]
                batch = [src[j] for j in idx]
                session, vocab = (
                    (self.groups[group].session, self.groups[group].vocab)
                    if group is not None
                    else (self.rec, self.vocab)
                )
                for j, (ids, conf) in zip(idx, run_ctc(session, batch)):
                    if conf <= best[j][1]:
                        continue
                    text = (
                        ""
                        if conf < MIN_CONF
                        else (vocab.decode_chars(ids) if group is not None else vocab.decode(ids))
                    )
                    best[j] = (text, conf)

        # Fallback for lines that yielded no text from specialized recognizers:
        # the base recognizer includes full Cyrillic, Latin, Greek, math and
        # punctuation, so a misrouted or out-of-vocabulary script is never
        # silently dropped.
        empty = [j for j, (text, _) in enumerate(best) if not text.strip()]
        if empty:
            src = tall if self.window > 1 else crops
            widths = [src[j].w for j in empty]
            for part in width_batches(widths):
                idx = [empty[i] for i in part]
                batch = [src[j] for j in idx]
                for j, (ids, conf) in zip(idx, run_ctc(self.rec, batch)):
                    if conf >= MIN_CONF:
                        text = self.vocab.decode(ids)
                        if text.strip() and conf > best[j][1]:
                            best[j] = (text, conf)
        return best

    def line_groups(self, crops: list[Crop]):
        """Per line: the group whose recognizer reads it (None = base) and
        whether the classifier is sure."""
        names: list[int | None] = [None] * len(crops)
        sure = [True] * len(crops)
        if self.script is None:
            return names, sure
        widths = [crop.w for crop in crops]
        for part in width_batches(widths):
            batch = [crops[j] for j in part]
            x = pad_batch(batch)
            feed = {
                "x": x,
                "widths": np.asarray([crop.w for crop in batch], dtype=np.int64),
            }
            out = _run(self.script, feed)
            p = next(iter(out.values()))
            for k, j in enumerate(part):
                row = p[k]
                arg = int(np.argmax(row))
                name = self.script_groups[arg] if arg < len(self.script_groups) else "base"
                names[j] = next(
                    (g for g, group in enumerate(self.groups) if group.name == name), None
                )
                sure[j] = float(np.max(row)) > ROUTE_SURE
        return names, sure


def run_ctc(session, batch: list[Crop]):
    """Runs a CTC recognizer on a batch and greedily decodes each line: repeats
    and blanks dropped, confidence = mean peak probability of what is kept."""
    x = pad_batch(batch)
    feed = {"x": x, "widths": np.asarray([crop.w for crop in batch], dtype=np.int64)}
    out = _run(session, feed)
    ids = out["ids"]
    conf = out["conf"]
    lengths = out.get("lengths")
    lengths_flat = np.asarray(lengths).ravel() if lengths is not None else None
    steps = ids.shape[1]
    results = []
    for k in range(len(batch)):
        n = min(max(int(lengths_flat[k]), 0), steps) if lengths_flat is not None else steps
        seq = ids[k, :n]
        peak = conf[k, :n]
        kept: list[int] = []
        total = 0.0
        for t in range(n):
            token = int(seq[t])
            if token != 0 and (t == 0 or token != int(seq[t - 1])):
                kept.append(token)
                total += float(peak[t])
        results.append((kept, total / len(kept) if kept else 0.0))
    return results


# ------------------------------------------------------------------- IndraEngine
# The chain adapter: name, preprocess mode and the OcrResult surface the rest
# of the OCR layer expects.

_CORE: Engine | None = None
_CORE_LOCK = threading.Lock()
_CORE_ERROR: str | None = None


def _core() -> Engine:
    """The shared pipeline, loaded on first use. A corrupt pack is probed
    once, not once per image; onnxruntime sessions are safe to run
    concurrently, which is how the indexer workers use it."""
    global _CORE, _CORE_ERROR
    if _CORE is not None:
        return _CORE
    if _CORE_ERROR is not None:
        raise RuntimeError(_CORE_ERROR)
    with _CORE_LOCK:
        if _CORE is not None:
            return _CORE
        try:
            threads = max(2, min(4, (os.cpu_count() or 4) // 2))
            _CORE = Engine.load(pack_dir(), threads)
        except Exception as exc:
            _CORE_ERROR = f"indra pack failed to load: {exc}"
            raise RuntimeError(_CORE_ERROR) from exc
        return _CORE


class IndraEngine:
    """The accurate tier: Indra behind the same Protocol as every other
    engine. `available()` is the cheap pack check; a pack that exists but
    cannot load fails at `recognize()`, and the caller falls through the
    chain — the same resilience model RapidOcrMulti uses."""

    name = "indra"
    preprocess_mode = "gray"
    label = "indra"

    def available(self) -> bool:
        return pack_present()

    def recognize(self, image) -> object:
        from .engine import OcrResult

        page = gray_from_image(image)
        text = _core().read(page)
        lines = [line for line in text.splitlines() if line.strip()]
        return OcrResult(text=text, engine=self.name, confidence=0.0, lines=lines)

