"""Text extraction dispatch by file kind."""

from __future__ import annotations

import logging
from pathlib import Path

from .archive import extract_archive
from .docx import extract_docx
from .email import extract_email
from .msgoutlook import extract_msg as extract_msgoutlook
from .office import (
    extract_epub,
    extract_opendocument,
    extract_pptx,
    extract_rtf,
    extract_xlsx,
)
from .pdf import extract_pdf
from .plain import extract_plain


def _extract_email_dispatch(path, max_bytes=20*1024*1024):
    """Route email extraction: .eml via email.py, .msg via msgoutlook.py."""
    if str(path).lower().endswith(".msg"):
        return extract_msgoutlook(path, max_bytes)
    return extract_email(path, max_bytes)


EXTRACTORS = {
    "text": extract_plain,
    "code": extract_plain,
    "docx": extract_docx,
    "pdf": extract_pdf,
    "pptx": extract_pptx,
    "xlsx": extract_xlsx,
    "epub": extract_epub,
    "odt": extract_opendocument,
    "rtf": extract_rtf,
    "archive": extract_archive,
    "email": _extract_email_dispatch,
    # `kind="msg"` is part of BUDGET_KINDS: route it through the same dispatcher
    # instead of silently returning '' for a missing key.
    "msg": _extract_email_dispatch,
}

PLAIN_KINDS = ("text", "code")
# .msg goes through the budgeted path too: one letter can hold a huge body.
BUDGET_KINDS = ("archive", "email", "msg")


def extract_text(
    path: str | Path,
    kind: str,
    max_bytes: int = 20 * 1024 * 1024,
    ocr: bool = True,
) -> str:
    extractor = EXTRACTORS.get(kind)
    if extractor is None:
        # Unknown extension: show real text when there is text to show. The
        # binary check in core/scanner.py already kept dll/exe/media out.
        if kind in ("other", ""):
            from pathlib import Path as _Path

            from ..scanner import MEDIA_EXT
            if _Path(path).suffix.lower() in MEDIA_EXT:
                return ""
            from .plain import extract_plain
            return extract_plain(path, max_bytes)
        return ""
    try:
        if kind == "pdf":
            # The scanned-PDF OCR is a caller decision: it is expensive, and
            # the indexer (and privacy settings) own whether it happens.
            return extract_pdf(path, max_bytes, ocr=ocr)
        if kind in PLAIN_KINDS:
            if str(path).lower().endswith((".csv", ".tsv")):
                # Row-shaped text beats raw CSV: a cell value search then
                # lands on its row. The kind stays "text" so the store and
                # the query facets do not grow a new surface for two
                # extensions.
                from .delimited import extract_delimited

                return extract_delimited(path, max_bytes)
            return extractor(path, max_bytes)  # type: ignore[call-arg]
        if kind in BUDGET_KINDS:
            return extractor(path, max_bytes)  # type: ignore[call-arg]
        return extractor(path)
    except Exception as exc:
        # A malformed package must never abort a whole indexing run. One handler
        # covers every parser failure; the debug line keeps programming errors
        # visible without turning a bad file into a failed run.
        logging.getLogger(__name__).debug(
            "extract %s (%s) failed: %s: %s", path, kind, type(exc).__name__, exc
        )
        return ""


__all__ = [
    "EXTRACTORS",
    "extract_archive",
    "extract_docx",
    "extract_email",
    "extract_epub",
    "extract_msgoutlook",
    "extract_opendocument",
    "extract_pdf",
    "extract_plain",
    "extract_pptx",
    "extract_rtf",
    "extract_text",
    "extract_xlsx",
]
