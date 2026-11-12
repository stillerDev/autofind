"""Text extraction dispatch by file kind."""

from __future__ import annotations

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
from .msg import extract_msg_file as _extract_msg_file

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
    "email": extract_email,
    "msg": _extract_msg_file,
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
    except (OSError, ValueError, KeyError):
        return ""
    except Exception:
        # A malformed package must never abort a whole indexing run.
        return ""


__all__ = [
    "extract_text",
    "EXTRACTORS",
    "extract_archive",
    "extract_docx",
    "extract_email",
    "extract_epub",
    "extract_opendocument",
    "extract_pdf",
    "extract_plain",
    "extract_pptx",
    "extract_rtf",
    "extract_xlsx",
    "extract_msgoutlook",
]
