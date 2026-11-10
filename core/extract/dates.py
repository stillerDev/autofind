"""Document dates out of the text itself.

A screenshot of a receipt or a scanned invoice carries its date inside the
pixels, so "documents from March" cannot be answered from mtime — the file
was created when it was screenshotted, not when the deal happened. One pass
of cheap regexes over the header of the text picks the first date that
actually parses, because documents put their date at the top. Day-first is
assumed for numeric forms (the corpus is ru/en): 05.03.2026 is March, not May.
"""

from __future__ import annotations

import re
from datetime import date

NUMERIC_RE = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\b")
ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
WORD_RE = re.compile(r"\b(\d{1,2})\s+([A-Za-zА-Яа-яЁё]{3,})\s+(\d{4})\b", re.UNICODE)

# Keys are regex prefixes: "март" matches "марта", "ма[йя]" matches "май" и "мая".
MONTHS: tuple[tuple[str, int], ...] = (
    ("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("ма[йя]", 5),
    ("июн", 6), ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10),
    ("ноябр", 11), ("декабр", 12),
    ("january", 1), ("february", 2), ("march", 3), ("april", 4), ("may", 5),
    ("june", 6), ("july", 7), ("august", 8), ("september", 9), ("october", 10),
    ("november", 11), ("december", 12),
)

WINDOW = 4000  # characters: the date lives in the header, not on page 40


def _valid(year: int, month: int, day: int) -> bool:
    return (
        1990 <= year <= date.today().year + 1
        and 1 <= month <= 12
        and 1 <= day <= 31
    )


def find_doc_date(text: str) -> str | None:
    """ISO ``YYYY-MM-DD`` for the first plausible date in the text, else None."""
    if not text:
        return None
    head = text[:WINDOW]

    match = ISO_RE.search(head)
    if match:
        year, month, day = (int(part) for part in match.groups())
        if _valid(year, month, day):
            return f"{year:04d}-{month:02d}-{day:02d}"

    for match in NUMERIC_RE.finditer(head):
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if year < 100:
            year += 2000
        if _valid(year, month, day):
            return f"{year:04d}-{month:02d}-{day:02d}"

    lowered = head.lower()
    for match in WORD_RE.finditer(lowered):
        day, name, year = int(match.group(1)), match.group(2), int(match.group(3))
        month = next((value for key, value in MONTHS if re.match(key, name)), None)
        if month is not None and _valid(year, month, day):
            return f"{year:04d}-{month:02d}-{day:02d}"
    return None
