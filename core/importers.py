"""Seed the index from other tools' file lists (Everything, Listary).

A file list from another search tool is a gift: it names hundreds of
thousands of paths with sizes and mtimes, none of which AutoFind needs to
re-crawl to make name search work. Two formats are read, both best-effort:

- **EFU** (Everything's "Export → EFU"): CSV with a
  ``Filename,Size,Date Modified,Date Created,Attributes`` header, dates in
  Windows FILETIME (100 ns ticks since 1601-01-01).
- **Listary**: its exported list is plain text, one absolute path per line
  (the tool has no documented interchange format, so anything that looks
  like a path is accepted).

Seeded rows carry an empty `sha256`: they have names, sizes and dates, but
no content yet. A later normal index run sees the file unchanged and only
then extracts text — the seed just makes the name layer instantly useful.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path

from .scanner import kind_for

# Windows FILETIME epoch is 1601-01-01; Unix is 1970-01-01. The difference
# is 11 644 473 600 seconds.
FILETIME_EPOCH_DELTA = 11_644_473_600

PATH_RE = re.compile(r"^[a-zA-Z]:\\[^\"|<>?\*\r\n]+$")


@dataclass(slots=True)
class Entry:
    path: str
    size: int
    mtime: float


def filetime_to_unix(ticks: int) -> float:
    """NTFS FILETIME ticks (100 ns since 1601) → Unix seconds."""
    return ticks / 10_000_000.0 - FILETIME_EPOCH_DELTA


def read_efu(path: str | Path) -> list[Entry]:
    """Everything's EFU export. Malformed rows are skipped, not fatal."""
    entries: list[Entry] = []
    with open(path, "r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            filename = (row.get("Filename") or row.get("filename") or "").strip()
            if not filename:
                continue
            try:
                size = int(row.get("Size") or 0)
            except (TypeError, ValueError):
                size = 0
            mtime = 0.0
            raw_time = str(row.get("Date Modified") or row.get("date_modified") or "").strip()
            try:
                # Everything writes FILETIME ticks; other exports carry an ISO
                # string or an RFC-2822 date (e.g. "Mon, 12 Nov 2026 18:24:00 +0000").
                # parsedate_to_datetime was imported but never used, so RFC dates
                # silently became mtime=0.0 and the later index run re-read them.
                if re.fullmatch(r"\d{10,}", raw_time):
                    mtime = filetime_to_unix(int(raw_time))
                elif raw_time:
                    from email.utils import parsedate_to_datetime

                    import datetime

                    try:
                        mtime = datetime.datetime.fromisoformat(
                            raw_time.replace("Z", "+00:00")
                        ).timestamp()
                    except ValueError:
                        mtime = parsedate_to_datetime(raw_time).timestamp()
            except (TypeError, ValueError, IndexError):
                mtime = 0.0
            entries.append(Entry(path=filename, size=size, mtime=mtime))
    return entries


def read_listary(path: str | Path) -> list[Entry]:
    """A Listary exported list: one absolute Windows path per line."""
    entries: list[Entry] = []
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip().strip('"')
            if not line or not PATH_RE.match(line):
                continue
            try:
                stat = Path(line).stat()
                entries.append(Entry(path=line, size=stat.st_size, mtime=stat.st_mtime))
            except OSError:
                entries.append(Entry(path=line, size=0, mtime=0.0))
    return entries


def detect_format(path: str | Path) -> str:
    """'efu' when the file starts with an EFU header, else 'listary'."""
    with open(path, "r", encoding="utf-8-sig", errors="replace") as handle:
        head = handle.read(4096)
    if "Filename" in head and ("Date Modified" in head or "Size" in head):
        return "efu"
    return "listary"


def read_list(path: str | Path, fmt: str = "auto") -> list[Entry]:
    if fmt == "auto":
        fmt = detect_format(path)
    if fmt == "efu":
        return read_efu(path)
    return read_listary(path)


def seed_index(store, entries: list[Entry]) -> dict:
    """Insert what the list knows into `files`; content extraction comes later.

    Rows already in the index keep their richer data — an upsert would only
    overwrite sha256/text with empties, so existing paths are counted as
    skipped instead.
    """
    added = 0
    skipped = 0
    for entry in entries:
        path = entry.path
        if store.get_file(path) is not None:
            skipped += 1
            continue
        store.upsert_file(
            path=path,
            size=entry.size,
            mtime=entry.mtime or 0.0,
            sha256="",  # no content yet: a later index run fills it
            kind=kind_for(path),
        )
        added += 1
    return {"added": added, "skipped": skipped}
