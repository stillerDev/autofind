"""Indexing pipeline: scan -> extract / OCR -> chunk -> embed -> store.

Documents and screenshots share one chunks table, so a single query returns a
PDF and a screenshot side by side. Images additionally get a thumbnail and a
visual vector, which is what powers "show me similar screenshots".
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .chunker import chunk_text
from .config import IndexConfig
from .extract import extract_text
from .images import load_image, thumbnail_bytes, visual_features
from .ocr import OcrUnavailable, ocr_image
from .scanner import iter_entries, sha256_file
from .store import Store

Progress = Callable[[int, int, str, str], None]


@dataclass(slots=True)
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0
    images: int = 0
    duplicates: int = 0
    ocr_empty: int = 0
    pruned: int = 0
    seconds: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)

    @property
    def files_per_second(self) -> float:
        return self.indexed / self.seconds if self.seconds > 0 else 0.0

    def as_dict(self) -> dict:
        return {
            "scanned": self.scanned,
            "indexed": self.indexed,
            "skipped": self.skipped,
            "failed": self.failed,
            "chunks": self.chunks,
            "images": self.images,
            "duplicates": self.duplicates,
            "ocr_empty": self.ocr_empty,
            "pruned": self.pruned,
            "seconds": round(self.seconds, 2),
            "files_per_second": round(self.files_per_second, 1),
        }


def run_index(
    roots: Sequence[str | Path],
    config: IndexConfig,
    store: Store,
    embedder,
    visual=None,
    prune: bool = False,
    progress: Progress | None = None,
    commit_every: int = 25,
) -> IndexStats:
    started = time.perf_counter()
    entries = list(
        iter_entries(
            roots,
            excluded_dirs=config.excluded_dirs,
            max_bytes=config.max_file_bytes,
            min_bytes=config.min_file_bytes,
        )
    )
    stats = IndexStats(scanned=len(entries))
    seen: set[str] = set()

    for position, entry in enumerate(entries, start=1):
        seen.add(entry.path)
        status = "index"
        try:
            existing = store.get_file(entry.path)
            if (
                existing
                and int(existing["size"]) == entry.size
                and abs(float(existing["mtime"]) - entry.mtime) < 1e-6
            ):
                stats.skipped += 1
                status = "unchanged"
            else:
                status = _index_one(entry, config, store, embedder, visual, stats)
        except Exception as exc:  # one broken file must not stop the run
            stats.failed += 1
            stats.errors.append((entry.path, f"{type(exc).__name__}: {exc}"))
            status = "error"

        if progress:
            progress(position, len(entries), status, entry.path)
        if position % commit_every == 0:
            store.commit()

    if prune:
        stats.pruned = store.prune(seen)
    store.commit()
    stats.seconds = time.perf_counter() - started
    return stats


def _index_one(entry, config: IndexConfig, store: Store, embedder, visual, stats: IndexStats) -> str:
    if entry.kind == "image":
        return _index_image(entry, config, store, embedder, visual, stats)

    digest = sha256_file(entry.path)
    existing = store.get_file(entry.path)
    if existing and existing["sha256"] == digest:
        store.upsert_file(entry.path, entry.size, entry.mtime, digest, entry.kind)
        stats.skipped += 1
        return "same-content"

    text = extract_text(entry.path, entry.kind, config.max_file_bytes)
    if not text.strip():
        stats.skipped += 1
        return "no-text"

    chunks = chunk_text(text, config.chunk_chars, config.chunk_overlap)
    vectors = embedder.encode_passages(chunks)
    file_id = store.upsert_file(entry.path, entry.size, entry.mtime, digest, entry.kind)
    stats.chunks += store.replace_chunks(file_id, chunks, vectors)
    stats.indexed += 1
    return "index"


def _index_image(entry, config: IndexConfig, store: Store, embedder, visual, stats: IndexStats) -> str:
    image = load_image(entry.path)
    if image is None:
        stats.skipped += 1
        return "undecodable"

    digest = sha256_file(entry.path)
    duplicate = store.image_by_sha(digest)
    if duplicate is not None and duplicate["path"] != entry.path:
        stats.duplicates += 1
        stats.skipped += 1
        return "duplicate"

    width, height = image.size
    file_id = store.upsert_file(entry.path, entry.size, entry.mtime, digest, "image")

    ocr_text = ""
    engine_name = None
    if config.ocr_enabled:
        try:
            result = ocr_image(entry.path, config.ocr_languages, config.ocr_engine)
            ocr_text = result.text
            engine_name = result.engine
        except OcrUnavailable:
            ocr_text = ""
    if not ocr_text.strip():
        stats.ocr_empty += 1

    chunks = chunk_text(ocr_text, config.chunk_chars, config.chunk_overlap) if ocr_text.strip() else []
    vectors = embedder.encode_passages(chunks) if chunks else None
    stats.chunks += store.replace_chunks(file_id, chunks, vectors)

    thumb = thumbnail_bytes(image, config.thumb_size)
    vector = visual.encode_image(image) if visual is not None else visual_features(image)
    store.upsert_image(
        entry.path, file_id, digest, width, height, entry.size, entry.mtime,
        thumb, ocr_text or None, engine_name, vector,
    )
    stats.images += 1
    stats.indexed += 1
    return "index-image"
