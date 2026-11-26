"""PDF extraction.

PyMuPDF is used when installed — an order of magnitude faster than pypdf, and
its text layer knows about more encodings. Without it pypdf takes over, and
without either a minimal fallback walks Flate streams and pulls strings out of
text operators: it handles simple text PDFs and returns '' for scanned or
CID-font documents, where OCR is the honest answer anyway.

Scanned PDFs: a document whose whole text layer is thinner than
SCANNED_TEXT_CHARS has no real text to search — it is pages of pixels with a
stamped page number at best. When OCR is enabled the pages are rendered to
bitmaps and read through the same OCR chain as screenshots (Windows packs
first, Indra/rapidocr behind it). This mirrors Kensaku's `pdf.rs`: same
threshold, the same per-engine page caps, the same text budget, and the same
"[Scanned PDF: image only]" marker that keeps the file indexed and the reason
visible in the UI. Page caps exist because a timed-out or stalled extraction
is retried by the watcher — a cap too high would retry forever.
"""

from __future__ import annotations

import logging
import re
import zlib
from pathlib import Path

STREAM_RE = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.S)
PIECE_RE = re.compile(rb"\((?:[^()\\]|\\.)*\)|<[0-9A-Fa-f\s]*>|T\*|Td|TD|Tj|TJ|ET")
ESCAPES = {
    b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f",
    b"(": b"(", b")": b")", b"\\": b"\\",
}

# Fewer than this many characters of real text across the whole document means
# scanned pages (a scanner banner or a stamped page number is why this is not
# "exactly empty").
SCANNED_TEXT_CHARS = 50

# A single page is never rendered above this many pixels, so a hostile PDF
# cannot allocate an unbounded buffer (the same budget Kensaku's pdf_image.rs
# applies to embedded images).
MAX_PAGE_PIXELS = 50_000_000

# What the UI shows when the pages could not be read: not silently empty.
SCANNED_MARKER = "[Scanned PDF: image only]"

_TRUNCATION_NOTE = "\n... [content truncated at limit]"

# The regex fallback scans raw bytes; a hostile 10 GB PDF must not be loaded
# whole just to look for a handful of text operators.
MAX_PDF_FALLBACK_BYTES = 8 * 1024 * 1024

# OCR pages per document, by the engine that will actually read them: a warm
# Windows pack costs ~10 ms a page, Indra ~1 s, rapidocr/tesseract ~1 s. The
# caps keep one PDF inside the extraction time budget the indexer tolerates.
_PAGES_PER_ENGINE = (
    ("indra", 10),
    ("windows-ocr", 20),
)
_DEFAULT_OCR_PAGES = 6


def _ocr_page_cap() -> int:
    """The page cap of the first engine that can run here, read once per
    scanned document (availability checks are cheap, sessions are not)."""
    try:
        from ..ocr.engine import available_engines

        engines = available_engines()
        name = engines[0].name if engines else ""
    except Exception:
        name = ""
    for prefix, cap in _PAGES_PER_ENGINE:
        if name.startswith(prefix):
            return cap
    return _DEFAULT_OCR_PAGES


def _render_page(page, zoom: float):
    """Page → PIL image under the pixel cap, largest zoom first: OCR quality
    is bought with pixels, so the cap only ever shrinks the zoom."""
    import pymupdf

    rect = page.rect
    scale = min(zoom, (MAX_PAGE_PIXELS / max(rect.width * rect.height, 1.0)) ** 0.5)
    scale = max(scale, 0.01)  # floor only against degenerate zero-zoom
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    from PIL import Image

    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    pixmap = None
    return image


def _ocr_scanned(document, max_bytes: int) -> tuple[str, bool]:
    """Render and read pages one at a time, in page order.

    Returns (text, unavailable). One page in memory at a time — decoding the
    whole document first held gigabytes before any OCR ran in the port this
    mirrors. `unavailable` tells the caller the engine could not run at all,
    which is worth a retry, unlike merely finding no words.
    """
    collected: list[str] = []
    size = 0
    unavailable = False
    cap = _ocr_page_cap()
    rendered = 0
    for page in document:
        if rendered >= cap or size >= max_bytes or unavailable:
            break
        try:
            image = _render_page(page, 3.0)  # ~216 dpi before the cap
        except Exception:
            continue  # one undecodable page is not a dead document
        rendered += 1
        try:
            from ..ocr import OcrUnavailable, ocr_page

            result = ocr_page(image)
        except OcrUnavailable:
            unavailable = True
            break
        except Exception:
            continue
        text = result.text.strip()
        if text:
            collected.append(text)
            size += len(text) + 2
    if unavailable:
        return "", True
    return "\n\n".join(collected), False


def extract_pdf(path: str | Path, max_bytes: int = 20 * 1024 * 1024, ocr: bool = True) -> str:
    try:
        import pymupdf  # type: ignore
    except ImportError:
        pymupdf = None

    partial = ""
    if pymupdf is not None:
        try:
            with pymupdf.open(str(path)) as document:
                page_count = len(document)
                pages: list[str] = []
                for page in document:
                    text = page.get_text("text")
                    if text and text.strip():
                        pages.append(text.strip())
                full_text = "\n".join(pages)
                partial = full_text

                scanned = page_count > 0 and len(full_text.strip()) < SCANNED_TEXT_CHARS
                try:
                    if scanned and ocr:
                        ocr_text, unavailable = _ocr_scanned(document, max_bytes)
                        if unavailable:
                            full_text = SCANNED_MARKER if not full_text.strip() else full_text
                        elif ocr_text:
                            full_text = (
                                ocr_text if not full_text.strip()
                                else f"{full_text}\n\n{ocr_text}"
                            )
                        elif not full_text.strip():
                            full_text = SCANNED_MARKER
                    elif scanned and not full_text.strip():
                        # OCR is off: keep the file indexed with an honest marker
                        # instead of dropping it as "no text".
                        full_text = SCANNED_MARKER
                except Exception as exc:
                    # A late OCR failure must not throw away the text layer (or
                    # the scanned marker) that was already assembled.
                    logging.getLogger(__name__).debug(
                        "scanned-page OCR failed: %s: %s", type(exc).__name__, exc
                    )
                partial = full_text
                if len(full_text) > max_bytes:
                    full_text = full_text[:max_bytes] + _TRUNCATION_NOTE
                partial = full_text
                if full_text.strip():
                    return full_text
        except Exception:
            pass

    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        PdfReader = None

    if PdfReader is not None:
        try:
            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
            if text.strip():
                return text
        except Exception:
            pass

    # PyMuPDF may have built text (including the scanned marker) before a late
    # error: keep it instead of silently dropping the file's search surface.
    if partial.strip():
        return partial

    try:
        with open(path, "rb") as handle:
            data = handle.read(min(int(max_bytes), MAX_PDF_FALLBACK_BYTES))
    except OSError:
        return ""
    return _minimal_text(data)


def _minimal_text(data: bytes) -> str:
    blocks: list[str] = []
    for match in STREAM_RE.finditer(data):
        content = _inflate(match.group(1))
        if b"Tj" not in content and b"TJ" not in content:
            continue
        block = _content_text(content)
        if block:
            blocks.append(block)
    return "\n".join(blocks).strip()


def _inflate(raw: bytes) -> bytes:
    for candidate in (raw, raw.strip(), raw.lstrip(b"\r\n")):
        try:
            return zlib.decompress(candidate)
        except zlib.error:
            continue
    try:
        return zlib.decompressobj().decompress(raw)
    except zlib.error:
        return raw


def _content_text(content: bytes) -> str:
    out: list[str] = []
    for piece in PIECE_RE.findall(content):
        if piece in (b"Td", b"TD", b"T*", b"ET"):
            out.append("\n")
        elif piece.startswith(b"("):
            out.append(_decode_literal(piece[1:-1]))
        elif piece.startswith(b"<"):
            hex_body = re.sub(rb"\s", b"", piece[1:-1])
            if len(hex_body) % 2:
                hex_body += b"0"
            try:
                out.append(bytes.fromhex(hex_body.decode("ascii")).decode("latin-1"))
            except ValueError:
                continue
    text = "".join(out)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def _decode_literal(raw: bytes) -> str:
    result = bytearray()
    index = 0
    while index < len(raw):
        char = raw[index:index + 1]
        if char != b"\\":
            result += char
            index += 1
            continue
        nxt = raw[index + 1:index + 2]
        if nxt in ESCAPES:
            result += ESCAPES[nxt]
            index += 2
        elif nxt.isdigit():
            octal = re.match(rb"[0-7]{1,3}", raw[index + 1:index + 4])
            result.append(int(octal.group(0), 8) & 0xFF if octal else 32)
            index += 1 + (len(octal.group(0)) if octal else 1)
        else:
            index += 2
    return result.decode("utf-8", errors="replace")
