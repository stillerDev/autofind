"""Grouping: file versions and screenshot bursts.

Two things make a result list look wrong on a real folder, and both are the same
problem — the same content appearing as several rows:

* **versions** — `resume.docx`, `resume_v2.docx`, `resume-final.docx` and
  `resume (1).docx` are one document the user has been editing. Listing four
  rows for it pushes the actual answer off the page.
* **bursts** — ten screenshots taken inside a minute of the same window, all
  matching the same query because they share OCR text.

Both are computed from names and from a 64-bit dHash, so they need no model and
no extra pass over the files: versions come from the path, bursts from the
`phash` column written at index time.
"""

from __future__ import annotations

import bisect
import re
from functools import lru_cache
from itertools import count
from dataclasses import dataclass, field
from typing import Iterable, Sequence

# A burst is a run of screenshots of the same thing: same folder, close in time,
# visually near-identical.
BURST_WINDOW_SECONDS = 15 * 60
BURST_MAX_DISTANCE = 10          # bits, out of 64
BURST_MAX_DISTANCE_BLANK = 2     # same, when there is no OCR text to cross-check
BURST_MIN_OCR_OVERLAP = 0.6
# A screenshot has a screen's worth of pixels. Sprite frames, icons and
# thumbnails sit far below this, and they are the main source of false bursts:
# forty animation frames of one character are near-identical by construction.
BURST_MIN_SIDE = 640

# Trailing noise that marks a version rather than a different document.
_VERSION_SUFFIXES = re.compile(
    r"(?ix)"
    r"(?:"
    r"[\s._-]*\((?P<paren>\d{1,3})\)"                       # file (1)
    r"|[\s._-]*\[(?P<bracket>\d{1,3})\]"                     # file [1]
    r"|[\s._-]*v(?:er(?:sion)?)?[\s._-]?\d{1,3}"            # v2, ver_3, version2
    r"|[\s._-]*в(?:ер(?:сия)?)?[\s._-]?\d{1,3}"             # в2, вер_3, версия2
    r"|[\s._-]*копия(?:\s*\(\d+\))?"
    r"|[\s._-]*copy(?:\s*\(\d+\))?"
    r"|[\s._-]*\(\s*копия\s*\)"
    r"|[\s._-]*(?:final|окончательн[^\s._-]*|итог[^\s._-]*)"
    r"|[\s._-]*(?:draft|черновик[^\s._-]*|wip)"
    r"|[\s._-]*(?:new|нов[^\s._-]*)"
    r"|[\s._-]*\d{4}[-_.]\d{2}[-_.]\d{2}"                   # date stamps
    r"|[\s._-]*\d{8}"
    r")+$"
)

# Words that carry no meaning on their own once the suffix is gone.
_GENERIC = re.compile(r"(?i)\b(?:final|copy|копия|версия|version|v)\b")


def stem(name: str) -> str:
    """Filename without extension."""
    base = name.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[0] if "." in base else base


def version_key(name: str) -> str:
    """Normalised stem: two names with the same key are versions of one file.

    Deliberately conservative — it only strips suffixes that are *known* version
    markers, because merging two genuinely different documents is worse than
    missing a merge. `report` and `report_2024` stay separate: the second is a
    different report far more often than it is a version.
    """
    base = stem(name).strip().lower()
    cleaned = base
    for _ in range(3):  # "final_v2 (1)" needs more than one pass
        stripped = _VERSION_SUFFIXES.sub("", cleaned).strip()
        if stripped == cleaned:
            break
        cleaned = stripped
    cleaned = _GENERIC.sub(" ", cleaned)
    cleaned = re.sub(r"[\s._-]+", " ", cleaned).strip()
    return cleaned or base


def folder(path: str) -> str:
    # lru_cache: the burst matcher asks for the same few folders hundreds of
    # thousands of times on screenshot-heavy queries (profiled at 1.8M rsplit
    # calls for one "screenshot" search) — a cache turns that into dict hits.
    return path.rsplit("\\", 1)[0] if "\\" in path else path.rsplit("/", 1)[0]


folder = lru_cache(maxsize=8192)(folder)


# ------------------------------------------------------------------ perceptual


def dhash(image) -> int:
    """64-bit difference hash: 9x8 grayscale, one bit per horizontal comparison.

    Cheap (one resize plus a comparison), stable against scaling and minor
    recompression, and good enough to tell "the same window again" from "a
    different window".
    """
    try:
        import numpy as np

        gray = np.asarray(image.convert("L").resize((9, 8), _resample()), dtype=np.int16)
    except Exception:
        return 0
    # One bit per (left > right) comparison, row-major: 8 rows x 8 pairs.
    bits = (gray[:, :-1] > gray[:, 1:]).reshape(-1)
    value = 0
    for position, flag in enumerate(bits.tolist()):
        if flag:
            value |= 1 << position
    # 63 bits, not 64: SQLite INTEGER is signed, and a hash with bit 63 set
    # cannot be stored at all (OverflowError, not a silent wrap). One bit of a
    # 64-bit layout hash is worth less than a column that works.
    return value & ((1 << 63) - 1)


def _resample():
    from PIL import Image

    return getattr(Image, "Resampling", Image).LANCZOS


def hamming(left: int, right: int) -> int:
    return int(left ^ right).bit_count()


def ocr_overlap(left: str, right: str) -> float:
    """Jaccard over word sets.

    Two images with no text at all count as a perfect match: there is nothing to
    disagree on, and they are visually identical by the time we get here. One
    with text and one without is a mismatch — that is a different screen.
    """
    a = set(left.lower().split())
    b = set(right.lower().split())
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------- grouping


@dataclass(slots=True)
class Group:
    """One row in the result list plus the rows it absorbed."""

    representative: object            # Hit
    members: list = field(default_factory=list)   # other Hits
    reason: str = ""

    @property
    def size(self) -> int:
        return len(self.members) + 1


def group_hits(hits: Sequence, bursts: bool = True, hashes: dict[int, str] | None = None) -> list[Group]:
    """Collapse versions, copies and bursts, keeping the best-ranked member visible.

    Input order is ranking order, so the first member of a group is the one the
    ranker preferred — a group never promotes a worse match just because it is
    newer.

    `hashes` maps file_id to sha256. With it, the same file copied into two
    folders becomes one row: `openWallpaper\\...\\x.txt` and `zz\\...\\x.txt` are
    one answer, not two. Restricted to the same name, because two different
    names with identical bytes are usually not interchangeable to the user.
    """
    result: list[Group] = []
    by_version: dict[tuple, Group] = {}
    by_copy: dict[tuple, Group] = {}
    # Burst anchors bucketed by (folder, width, height) — the three things a
    # burst match demands to be identical — and each bucket sorted by mtime so
    # the 15-minute window is a bisect, not a scan. The flat list this replaced
    # made a screenshot-heavy query O(anchors) per hit: one "screenshot" search
    # on this corpus profiled at 3.3k hits x ~550 anchors, 1.8M rsplit calls.
    anchors: dict[tuple, list] = {}
    seq = count()

    for hit in hits:
        width = getattr(hit, "width", 0)
        height = getattr(hit, "height", 0)
        burstable = (
            bursts and hit.kind == "image" and getattr(hit, "phash", 0)
            and width >= BURST_MIN_SIDE and height >= BURST_MIN_SIDE
        )
        if burstable:
            anchor = _match_burst(hit, anchors.get((folder(hit.path), width, height)))
            if anchor is not None:
                anchor.members.append(hit)
                anchor.reason = "burst"
                continue

        key = ("version", folder(hit.path), version_key(hit.name))
        existing = by_version.get(key)
        if existing is not None:
            existing.members.append(hit)
            existing.reason = "version"
            continue

        digest = (hashes or {}).get(hit.file_id)
        copy_key = ("copy", hit.name.lower(), digest) if digest else None
        existing = by_copy.get(copy_key) if copy_key else None
        if existing is not None:
            existing.members.append(hit)
            existing.reason = "copy"
            continue

        group = Group(representative=hit, reason="version")
        by_version[key] = group
        if copy_key is not None:
            by_copy[copy_key] = group
        result.append(group)
        if burstable:
            # (mtime, seq, group): the counter keeps the tuple comparable when
            # two screenshots share a timestamp.
            entry = (hit.mtime, next(seq), group)
            bisect.insort(anchors.setdefault((folder(hit.path), width, height), []), entry)
    return result


def _match_burst(hit, bucket: Sequence[tuple] | None) -> Group | None:
    """First image group this screenshot is a repeat of, if any.

    The bucket is (folder, width, height) and sorted by mtime, so every entry
    in the bisected slice already shares the pixel grid and sits inside the
    time window; the remaining checks — dHash distance and OCR overlap — run
    only on those.
    """
    if not bucket:
        return None
    phash = getattr(hit, "phash", 0)
    lo = bisect.bisect_left(bucket, (hit.mtime - BURST_WINDOW_SECONDS,))
    hi = bisect.bisect_right(bucket, (hit.mtime + BURST_WINDOW_SECONDS + 1,))
    for _, _, group in bucket[lo:hi]:
        other = group.representative
        distance = hamming(phash, getattr(other, "phash", 0))
        if distance > BURST_MAX_DISTANCE:
            continue
        # A near-identical image in the same window is the same screen only if
        # it also says the same thing; two blank windows look alike. With no
        # text on either side there is nothing to confirm the match, so demand
        # a much tighter hash — flat icons collide at 8-10 bits routinely.
        overlap = ocr_overlap(other.snippet, hit.snippet)
        if overlap < BURST_MIN_OCR_OVERLAP:
            continue
        if overlap >= 1.0 and not other.snippet.strip() and not hit.snippet.strip():
            if distance > BURST_MAX_DISTANCE_BLANK:
                continue
        return group
    return None


def describe(groups: Sequence[Group]) -> dict[str, int]:
    return {
        "groups": len(groups),
        "collapsed": sum(group.size - 1 for group in groups),
    }
