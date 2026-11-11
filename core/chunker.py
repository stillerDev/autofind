"""Sentence-aware chunking.

Chunks are sized in characters, tuned to land inside the 300-500 token window
the plan asks for (Russian averages ~3 characters per token). Splitting prefers
sentence ends, then word boundaries, so a chunk never starts mid-word.

Since schema v9 the text is stored **once per file** and a chunk is only a span
into it, so `chunk_spans()` is the primitive: `(start, end)` pairs into the
normalised text. `chunk_text()` stays as the string-shaped wrapper the older
callers and tests use.
"""

from __future__ import annotations

import re

SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…;:])\s+|\n{2,}")


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def chunk_spans(
    text: str, chunk_chars: int = 1200, overlap: int = 200
) -> list[tuple[int, int]]:
    """Half-open spans into the *normalised* text, one per chunk.

    The spans are real regions of the source: `text[start:end]` is the chunk,
    with the original line breaks and spacing intact. Overlap is expressed by
    starting the next span a little before the previous one ended, which is the
    same window `_tail` used to prepend — only now it is a slice, not a copy.
    """
    text = normalize(text)
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [(0, len(text))]

    spans: list[tuple[int, int]] = []
    buf_start: int | None = None
    buf_end = 0
    length = 0

    for s_start, s_end in _sentence_spans(text):
        if s_end - s_start > chunk_chars:
            if buf_start is not None:
                spans.append((buf_start, buf_end))
                buf_start, length = None, 0
            spans.extend(_hard_split_spans(text, s_start, s_end, chunk_chars, overlap))
            continue
        if length + (s_end - s_start) + 1 > chunk_chars and buf_start is not None:
            spans.append((buf_start, buf_end))
            tail = _tail_start(text, buf_end, overlap)
            if tail is None:
                buf_start, length = None, 0
            else:
                buf_start, buf_end = tail, buf_end
                length = buf_end - buf_start
        if buf_start is None:
            buf_start, buf_end = s_start, s_end
        else:
            buf_end = s_end
        length += (s_end - s_start) + 1

    if buf_start is not None:
        spans.append((buf_start, buf_end))
    return [(start, end) for start, end in spans if end > start]


def chunk_text(text: str, chunk_chars: int = 1200, overlap: int = 200) -> list[str]:
    normalised = normalize(text)
    return [
        normalised[start:end]
        for start, end in chunk_spans(normalised, chunk_chars, overlap)
    ]


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Sentences as spans, trimmed the way `str.strip()` trimmed them before."""
    spans: list[tuple[int, int]] = []
    position = 0
    for match in SENTENCE_BOUNDARY.finditer(text):
        _append_trimmed(spans, text, position, match.start())
        position = match.end()
    _append_trimmed(spans, text, position, len(text))
    return spans


def _append_trimmed(
    spans: list[tuple[int, int]], text: str, start: int, end: int
) -> None:
    piece = text[start:end]
    stripped = piece.strip()
    if not stripped:
        return
    leading = len(piece) - len(piece.lstrip())
    spans.append((start + leading, start + leading + len(stripped)))


def _hard_split_spans(
    text: str, start: int, end: int, chunk_chars: int, overlap: int
) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    position = start
    while position < end:
        stop = min(position + chunk_chars, end)
        if stop < end:
            pivot = text.rfind(" ", position + chunk_chars // 2, stop)
            if pivot != -1:
                stop = pivot
        trimmed_start, trimmed_end = _trim(text, position, stop)
        if trimmed_end > trimmed_start:
            spans.append((trimmed_start, trimmed_end))
        if stop >= end:
            break
        position = max(stop - overlap, position + 1)
    return spans


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    piece = text[start:end]
    stripped = piece.strip()
    if not stripped:
        return start, start
    leading = len(piece) - len(piece.lstrip())
    return start + leading, start + leading + len(stripped)


def _tail_start(text: str, end: int, overlap: int) -> int | None:
    """Where the overlap tail of the previous chunk begins, or None for no tail.

    Mirrors `_tail`: the last `overlap` characters, cut after the first space so
    the next chunk does not start mid-word.
    """
    if overlap <= 0 or end <= overlap:
        return None
    start = end - overlap
    space = text.find(" ", start, end)
    return space + 1 if space != -1 else start
