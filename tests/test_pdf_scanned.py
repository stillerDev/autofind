"""Scanned-PDF OCR: the parity port of Kensaku's `pdf.rs` policy.

A PDF whose whole text layer is under 50 characters is pages of pixels; those
pages are rendered and read through the same OCR chain as screenshots. The
threshold, page caps, text budget and the "[Scanned PDF: image only]" marker
all mirror the Rust original, so the fixtures here build real scanned PDFs
with PyMuPDF and run real OCR on them.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw, ImageFont

from core.extract import extract_text
from core.extract.pdf import (
    MAX_PAGE_PIXELS,
    SCANNED_MARKER,
    SCANNED_TEXT_CHARS,
    _ocr_page_cap,
    _render_page,
    extract_pdf,
)


def _text_png(path: Path, text: str) -> None:
    image = Image.new("RGB", (900, 320), (250, 250, 250))
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except OSError:
        font = ImageFont.load_default()
    draw.text((40, 60), text, font=font, fill=(20, 20, 30))
    image.save(path)


def _scanned_pdf(path: Path, text: str) -> None:
    """A real scanned document: one full-page raster, no text layer."""
    import pymupdf

    png = path.with_suffix(".png")
    _text_png(png, text)
    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_image(page.rect, filename=str(png))
    document.save(str(path))
    document.close()
    png.unlink()


def _text_pdf(path: Path, text: str) -> None:
    import pymupdf

    document = pymupdf.open()
    page = document.new_page(width=612, height=792)
    page.insert_text((72, 100), text, fontsize=12)
    document.save(str(path))
    document.close()


class ScannedPdfTests(unittest.TestCase):
    def test_a_scanned_pdf_is_read_through_the_ocr_chain(self):
        if not Path("C:/Windows/Fonts/arial.ttf").exists():
            self.skipTest("no font to draw the fixture")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.pdf"
            _scanned_pdf(path, "Invoice 2026 totals")
            text = extract_text(path, "pdf")
        self.assertIn("Invoice", text)

    def test_ocr_disabled_leaves_the_honest_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.pdf"
            _scanned_pdf(path, "Invoice 2026 totals")
            text = extract_text(path, "pdf", ocr=False)
        self.assertEqual(text, SCANNED_MARKER, "OCR off: the file stays indexed, marked")

    def test_text_rich_pdf_never_touches_ocr(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "text.pdf"
            _text_pdf(path, "Real selectable prose " * 10)
            with mock.patch("core.extract.pdf._ocr_scanned") as ocr_spy:
                text = extract_text(path, "pdf")
        self.assertNotIn(SCANNED_MARKER, text)
        self.assertGreater(len(text.strip()), SCANNED_TEXT_CHARS)
        ocr_spy.assert_not_called()

    def test_thin_text_still_counts_as_scanned(self):
        # A stamp ("3") under the threshold with an image behind it: scanned.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stamped.pdf"
            _scanned_pdf(path, "Invoice 2026 totals")
            # A text layer with just a page number on top of the raster.
            import pymupdf

            document = pymupdf.open(str(path))
            document[0].insert_text((72, 700), "3", fontsize=10)
            document.save(str(path.with_name("stamped2.pdf")))
            document.close()
            with mock.patch(
                "core.extract.pdf._ocr_scanned", return_value=("ocr text", False)
            ) as ocr_spy:
                text = extract_text(path.with_name("stamped2.pdf"), "pdf")
        ocr_spy.assert_called_once()
        self.assertEqual(text, "3\n\nocr text", "text layer kept, OCR appended")

    def test_ocr_unavailable_falls_back_to_the_marker(self):
        from core.ocr import OcrUnavailable

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.pdf"
            _scanned_pdf(path, "Invoice 2026 totals")
            with mock.patch(
                "core.extract.pdf._ocr_scanned",
                side_effect=lambda _doc, _cap: ("", True),
            ):
                text = extract_text(path, "pdf")
        self.assertEqual(text, SCANNED_MARKER)

    def test_text_budget_truncates_the_ocr_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scan.pdf"
            _scanned_pdf(path, "Invoice 2026 totals")
            with mock.patch(
                "core.extract.pdf._ocr_scanned",
                return_value=("word " * 500, False),
            ):
                text = extract_pdf(path, max_bytes=64)
        self.assertIn("[content truncated at limit]", text)
        self.assertLess(len(text), 200)

    def test_render_respects_the_pixel_cap(self):
        import pymupdf

        document = pymupdf.open()
        page = document.new_page(width=20000, height=20000)  # absurd page
        with mock.patch.object(
            __import__("core.extract.pdf", fromlist=["MAX_PAGE_PIXELS"]),
            "MAX_PAGE_PIXELS",
            1_000_000,
        ):
            image = _render_page(page, 3.0)
        document.close()
        self.assertLessEqual(image.width * image.height, 1_000_000)

    def test_page_cap_follows_the_first_engine(self):
        # The chain: windows packs are ~10 ms a page (cap 20); anything ~1 s
        # a page gets the conservative 6.
        self.assertIn(_ocr_page_cap(), (6, 10, 20))


if __name__ == "__main__":
    unittest.main()
