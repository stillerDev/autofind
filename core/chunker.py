"""Sentence-aware chunking.

Chunks are sized in characters, tuned to land inside the 300-500 token window
the plan asks for (Russian averages ~3 characters per token). Splitting prefers
sentence ends, then word boundaries, so a chunk never starts mid-word.
"""

from __future__ import annotations

import re

SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?…;:])\s+|\n{2,}")


def chunk_text(text: str, chunk_chars: int = 1200, overlap: int = 200) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if len(text) <= chunk_chars:
        return [text]

    sentences = [part.strip() for part in SENTENCE_BOUNDARY.split(text) if part and part.strip()]
    chunks: list[str] = []
    buffer: list[str] = []
    length = 0

    for sentence in sentences:
        if len(sentence) > chunk_chars:
            if buffer:
                chunks.append(" ".join(buffer))
                buffer, length = [], 0
            chunks.extend(_hard_split(sentence, chunk_chars, overlap))
            continue
        if length + len(sentence) + 1 > chunk_chars and buffer:
            chunks.append(" ".join(buffer))
            tail = _tail(chunks[-1], overlap)
            buffer = [tail] if tail else []
            length = len(tail)
        buffer.append(sentence)
        length += len(sentence) + 1

    if buffer:
        chunks.append(" ".join(buffer))
    return [chunk for chunk in chunks if chunk.strip()]


def _hard_split(text: str, chunk_chars: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + chunk_chars, len(text))
        if end < len(text):
            pivot = text.rfind(" ", start + chunk_chars // 2, end)
            if pivot != -1:
                end = pivot
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [piece for piece in pieces if piece]


def _tail(chunk: str, overlap: int) -> str:
    if overlap <= 0 or len(chunk) <= overlap:
        return ""
    tail = chunk[-overlap:]
    pivot = tail.find(" ")
    return tail[pivot + 1:] if pivot != -1 else tail
