"""Script detection and CJK routing for OCR.

The Windows recogniser ships with the OS and handles Russian well, but it only
knows the scripts whose language packs are installed — on a stock machine that
is `en-US` and whatever the user's display language is. A Chinese or Japanese
screenshot then comes back as a handful of stray Latin characters, which is
worse than no text at all: it looks indexed but is not findable.

So OCR here is two-tier. The fast, always-present engine runs first; if its
output is implausibly small for the amount of ink on the image, the image is
re-run through a CJK-capable engine and the better result wins. "Better" is
measured, not guessed, so the router cannot make a screenshot worse than it was
before CJK support existed.
"""

from __future__ import annotations

import unicodedata

CJK_RANGES = (
    (0x3040, 0x30FF),   # hiragana, katakana
    (0x3400, 0x4DBF),   # CJK extension A
    (0x4E00, 0x9FFF),   # CJK unified ideographs
    (0xF900, 0xFAFF),   # compatibility ideographs
    (0xAC00, 0xD7AF),   # hangul syllables
    (0x1100, 0x11FF),   # hangul jamo
)

# Windows OCR language tags that read CJK, best first. `zh-Hans` also covers the
# Japanese kanji a `ja` pack would, so it is the fallback when `ja` is absent.
CJK_TAGS = ("ja", "zh-Hans", "zh-Hant", "ko")

# A failed read does not return "less text", it returns a handful of stray
# Latin glyphs — measured at 0-5 characters on a 1080p Chinese screenshot. So the
# test is an absolute floor with a small area term, not a density: a sparse
# settings dialog with one heading must not trigger a second pass.
MIN_EXPECTED_CHARS = 8.0
EXPECTED_PER_MEGAPIXEL = 2.0

# An engine result must be at least this much better to replace the first one.
MIN_IMPROVEMENT = 1.6


def is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in CJK_RANGES)


def cjk_count(text: str) -> int:
    return sum(1 for char in text if is_cjk(char))


def cjk_ratio(text: str) -> float:
    meaningful = [char for char in text if not char.isspace()]
    if not meaningful:
        return 0.0
    return cjk_count(text) / len(meaningful)


def text_score(text: str) -> float:
    """How much usable text a result contains.

    Counts letters and digits and gives CJK characters double weight: one
    ideograph carries about as much information as a short word, and the whole
    point of the comparison is that a Chinese screenshot read as three Latin
    letters must lose to a real transcription.
    """
    score = 0.0
    for char in text:
        if char.isspace():
            continue
        if is_cjk(char):
            score += 2.0
        elif unicodedata.category(char)[0] in "LN":
            score += 1.0
        elif char.isprintable():
            score += 0.1
    return score


def looks_unread(text: str, width: int, height: int) -> bool:
    """Did the engine fail to read this image, rather than find little in it?"""
    if not text.strip():
        return True
    megapixels = max(width * height, 1) / 1_000_000.0
    expected = MIN_EXPECTED_CHARS + EXPECTED_PER_MEGAPIXEL * megapixels
    return text_score(text) < expected


def better(first: str, second: str) -> bool:
    """Is `second` enough better than `first` to replace it?"""
    return text_score(second) > text_score(first) * MIN_IMPROVEMENT
