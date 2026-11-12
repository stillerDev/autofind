"""CSV and TSV extraction, shaped like `pandas.head`.

Each data row becomes one line of `column = "value"` pairs, so a search for a
column name or a cell value lands on the row that holds it — raw CSV text
would leave the header a screenful away from the row that matters. A column
summary (names and the type the sampled rows agree on) comes first, Kensaku
`delimited.rs` style: same 200-row head, the same sniffing rule, the same
labels.

Only the start of the file is parsed (a capped read plus the row cap), so a
multi-gigabyte export costs about as much as a small one.
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

# Only the start of the file is used to choose the delimiter.
SNIFF_BYTES = 8 * 1024
# How many records are compared when choosing the delimiter.
SNIFF_RECORDS = 20
# Data rows kept. The cap is what makes a 10 GB export cheap to index.
HEAD_ROWS = 200
# A very wide header must not outgrow the row text it describes.
MAX_SUMMARY_CHARS = 8 * 1024

# Delimiter candidates, hint first (`.tsv` prefers tab): ties go to the
# earlier candidate, so the extension's favourite is tried before the rest.
_CANDIDATES = (";", "\t", ",")


def _decode(data: bytes) -> str:
    """UTF-16 by BOM when present, else UTF-8 with errors replaced; a NUL
    byte with no BOM rejects the file as binary, the same check plain text
    extraction applies."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return data.decode("utf-16").lstrip("\ufeff")
        except (UnicodeDecodeError, ValueError):
            return ""
    if b"\x00" in data[:4096]:
        return ""
    return data.decode("utf-8", errors="replace").lstrip("\ufeff")


def _records(text: str, delimiter: str):
    """Parsed records as lists of fields; the reader stops at a malformed row
    instead of raising, because a spreadsheet export is read, not obeyed."""
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    while True:
        try:
            row = next(reader)
        except StopIteration:
            return
        except csv.Error:
            return
        yield row


def _sniff(text: str, hint: str) -> str:
    """The delimiter whose first records agree on the most fields; ties go to
    the earlier candidate, so the hint is tried first."""
    order = (hint,) + tuple(c for c in _CANDIDATES if c != hint)
    best = (hint, 0, 0)
    sample = text[:SNIFF_BYTES]
    for candidate in order:
        width = 0
        agreeing = 0
        seen = 0
        for fields in _records(sample, candidate):
            if seen == 0:
                width = len(fields)
            if len(fields) == width:
                agreeing += 1
            seen += 1
            if seen >= SNIFF_RECORDS:
                break
        if width >= 2 and (agreeing, width) > (best[1], best[2]):
            best = (candidate, agreeing, width)
    return best[0]


def _column_name(raw: str, index: int) -> str:
    name = raw.strip()
    return name if name else f"col{index + 1}"


def _cell_kind(value: str, kind: str) -> str:
    """'empty' | 'integer' | 'number' | 'text', merged over the sampled rows
    (one float anywhere turns an integer column into a number column)."""
    value = value.strip()
    if not value or kind == "text":
        return kind
    if _is_int(value):
        return "integer" if kind in ("empty", "integer") else "number"
    if _is_float(value):
        return "number" if kind in ("empty", "integer", "number") else "text"
    return "text"


def _is_int(value: str) -> bool:
    try:
        int(value)
        return True
    except ValueError:
        return False


def _is_float(value: str) -> bool:
    try:
        float(value)
        return True
    except ValueError:
        return False


def _one_line(value: str) -> str:
    # Newlines inside a cell would split one record into two search results.
    return " ".join(value.split())


def _label(kind: str) -> str:
    return {"empty": "empty", "integer": "integer", "number": "number"}.get(kind, "text")


def extract_delimited(
    path: str | Path, max_bytes: int = 20 * 1024 * 1024, head_rows: int = HEAD_ROWS
) -> str:
    """Column summary and the first `head_rows` rows as `column = "value"`."""
    try:
        data = Path(path).read_bytes()[:max_bytes]
        truncated = Path(path).stat().st_size > len(data)
    except OSError:
        return ""
    text = _decode(data)
    if not text.strip():
        return ""
    if truncated:
        # The capped read may end mid-record: keep only records the file
        # actually finished (the same rule Kensaku applies to its take()).
        cut = text.rfind("\n")
        if cut != -1:
            text = text[:cut]
    delimiter = _sniff(text, "\t" if str(path).lower().endswith(".tsv") else ",")

    names: list[str] | None = None
    kinds: list[str] = []
    rows: list[str] = []
    out_size = 0
    sampled = False

    for fields in _records(text, delimiter):
        if names is None:
            names = [_column_name(raw, index) for index, raw in enumerate(fields)]
            kinds = ["empty"] * len(names)
            continue
        if len(rows) >= head_rows:
            sampled = True
            break
        for index, kind in enumerate(kinds):
            kinds[index] = _cell_kind(
                fields[index] if index < len(fields) else "", kind
            )
        line = ", ".join(
            f'{names[index] if index < len(names) else f"col{index + 1}"} = "{_one_line(value)}"'
            for index, value in enumerate(fields)
        )
        if out_size + len(line) > max_bytes and rows:
            break  # the row text budget is spent
        rows.append(line)
        out_size += len(line) + 1

    if not rows:
        # No data rows: index the column names so they stay searchable.
        return ", ".join(names) if names else ""

    summary = "columns (first rows sampled): " if sampled else "columns: "
    summary += ", ".join(
        f"{name} ({_label(kind)})" for name, kind in zip(names or [], kinds)
    )
    return f"{summary[:MAX_SUMMARY_CHARS]}\n" + "\n".join(rows)
