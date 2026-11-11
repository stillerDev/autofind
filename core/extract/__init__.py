"""Text extraction dispatch by file kind."""

from __future__ import annotations

from pathlib import Path

from .archive import extract_archive
from .docx import extract_docx
from .email import extract_email
from .office import (
    extract_epub,
    extract_opendocument,
    extract_pptx,
    extract_rtf,
    extract_xlsx,
)
from .pdf import extract_pdf
from .plain import extract_plain

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
}

PLAIN_KINDS = ("text", "code")
BUDGET_KINDS = ("archive", "email")


def extract_text(path: str | Path, kind: str, max_bytes: int = 20 * 1024 * 1024) -> str:
    extractor = EXTRACTORS.get(kind)
    if extractor is None:
        return ""
    try:
        if kind in PLAIN_KINDS or kind in BUDGET_KINDS:
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
]
