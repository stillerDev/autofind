"""Text extraction dispatch by file kind."""

from __future__ import annotations

from pathlib import Path

from .docx import extract_docx
from .pdf import extract_pdf
from .plain import extract_plain

EXTRACTORS = {
    "text": extract_plain,
    "code": extract_plain,
    "docx": extract_docx,
    "pdf": extract_pdf,
}


def extract_text(path: str | Path, kind: str, max_bytes: int = 20 * 1024 * 1024) -> str:
    extractor = EXTRACTORS.get(kind)
    if extractor is None:
        return ""
    try:
        if kind in ("text", "code"):
            return extractor(path, max_bytes)  # type: ignore[call-arg]
        return extractor(path)
    except (OSError, ValueError):
        return ""


__all__ = ["extract_text", "EXTRACTORS", "extract_docx", "extract_pdf", "extract_plain"]
