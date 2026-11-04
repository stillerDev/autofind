"""Indexing pipeline: scan -> extract / OCR -> chunk -> embed -> store.

Documents and screenshots share one chunks table, so a single query returns a
PDF and a screenshot side by side. Images additionally get a thumbnail and a
visual vector, which is what powers "show me similar screenshots".

Extraction runs in a pool of worker processes (see `_prepared_entries`): the
cost per file is dominated by PDF/Office parsing and OCR, which is where the
seconds go, while the main process keeps the one thing that must stay single
-owner — the SQLite connection — and only writes. Embedding moved into the
workers too: a batch of chunks is cheap to pickle next to the text it came
from, and the main process no longer waits on the model at all.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from .chunker import chunk_text
from .config import IndexConfig
from .embed import get_embedder, get_visual_embedder
from .extract import extract_text
from .group import dhash
from .images import load_image, thumbnail_bytes, visual_features
from .ocr import OcrUnavailable, ocr_image, reset_fallbacks
from .scanner import iter_entries, sha256_file
from .store import Store
from .symbols import extract_for

Progress = Callable[[int, int, str, str], None]


@dataclass(slots=True)
class IndexStats:
    scanned: int = 0
    indexed: int = 0
    skipped: int = 0
    failed: int = 0
    chunks: int = 0
    images: int = 0
    symbols: int = 0
    duplicates: int = 0
    ocr_empty: int = 0
    rehashed: int = 0
    pruned: int = 0
    sensitive: int = 0
    sensitive_examples: list[tuple[str, str]] = field(default_factory=list)
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
            "symbols": self.symbols,
            "duplicates": self.duplicates,
            "ocr_empty": self.ocr_empty,
            "rehashed": self.rehashed,
            "pruned": self.pruned,
            "sensitive": self.sensitive,
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
    workers: int | None = None,
) -> IndexStats:
    started = time.perf_counter()
    # A folder of photographs teaches the OCR fallback chain nothing; a folder of
    # screenshots teaches it everything. Start each run with a clean slate so the
    # verdict from the previous folder does not carry over.
    reset_fallbacks()
    blocked: list[tuple[str, str]] = []
    entries = list(
        iter_entries(
            roots,
            excluded_dirs=config.excluded_dirs,
            max_bytes=config.max_file_bytes,
            min_bytes=config.min_file_bytes,
            privacy=config.privacy and not config.allow_sensitive,
            skipped=blocked,
        )
    )
    stats = IndexStats(scanned=len(entries), sensitive=len(blocked), sensitive_examples=blocked[:5])
    seen: set[str] = set()

    # The cheap skip (size+mtime unchanged) needs the store, so it stays in the
    # main process and prunes the pool's workload before any process pays for a
    # spawn.
    todo = []
    seen_sha: dict[str, str] = {}  # digest -> path dispatched earlier in this run
    for position, entry in enumerate(entries, start=1):
        seen.add(entry.path)
        try:
            existing = store.get_file(entry.path)
            if (
                existing
                and int(existing["size"]) == entry.size
                and abs(float(existing["mtime"]) - entry.mtime) < 1e-6
            ):
                stats.skipped += 1
                if progress:
                    progress(position, len(entries), "unchanged", entry.path)
                continue
            if entry.kind == "image":
                # A byte-identical screenshot is skipped before the pool gets
                # it: the digest costs one file read, while the OCR the worker
                # would run first costs hundreds of milliseconds per copy. The
                # in-memory map catches copies inside this same run, which the
                # store cannot know about yet.
                digest = sha256_file(entry.path)
                duplicate = store.image_by_sha(digest)
                if duplicate is not None and duplicate["path"] != entry.path:
                    stats.duplicates += 1
                    stats.skipped += 1
                    if progress:
                        progress(position, len(entries), "duplicate", entry.path)
                    continue
                first = seen_sha.get(digest)
                if first is not None and first != entry.path:
                    stats.duplicates += 1
                    stats.skipped += 1
                    if progress:
                        progress(position, len(entries), "duplicate", entry.path)
                    continue
                seen_sha[digest] = entry.path
        except Exception as exc:
            stats.failed += 1
            stats.errors.append((entry.path, f"{type(exc).__name__}: {exc}"))
            continue
        todo.append((position, entry))

    done_count = len(entries) - len(todo)
    _run_extraction(todo, config, store, embedder, visual, stats, progress, commit_every, workers, done_count)

    if prune:
        stats.pruned = store.prune(seen)
    store.commit()
    # Old indexes predate `images.phash`, and an unchanged file is never
    # re-indexed, so without this the burst detector silently sees no hashes on
    # exactly the screenshots that were there first. Cheap: decode only, no OCR.
    stats.rehashed = backfill_phash(store)
    store.commit()
    # Same problem, same shape: a code file indexed before the symbols table
    # existed is never re-indexed, so `sym:` would find nothing in exactly the
    # files that were there first. One pass, guarded by a meta flag.
    stats.symbols += backfill_symbols(store)
    store.commit()
    # Build the memory-mapped vector pack now, not on the first query. The
    # indexer already has everything on disk; the alternative is a 2.6 s stall
    # the first time someone searches after an index run.
    if store.vector_backend == "pack":
        store.rebuild_pack()
    stats.seconds = time.perf_counter() - started
    return stats


HEAVY_KINDS = frozenset({"image", "pdf", "docx", "pptx", "xlsx", "epub", "odt"})


def _worth_a_pool(todo: Sequence[tuple[int, object]], neural: bool) -> bool:
    """Is there enough expensive work to pay for spawning the workers?

    Each Windows spawn re-imports numpy and PIL — a few hundred milliseconds
    per worker, once. A hundred plain-text files extract in less time than
    that; four PDFs or screenshots already cost seconds each, so a handful of
    heavy files tips the balance long before a pile of light ones does. A
    neural embedder counts as heavy work too: its forward pass moves into the
    workers with the text.
    """
    heavy = sum(1 for _, entry in todo if entry.kind in HEAVY_KINDS)
    return heavy >= 4 or len(todo) >= 40 and neural


def _run_extraction(
    todo: Sequence[tuple[int, object]],
    config: IndexConfig,
    store: Store,
    embedder,
    visual,
    stats: IndexStats,
    progress: Progress | None,
    commit_every: int,
    workers: int | None,
    done_before: int,
) -> None:
    """Extract in parallel, write serially.

    The pool owns everything that reads files and runs models; the main loop
    owns the store. A pool that dies (a crashed OCR engine, a spawn problem)
    demotes the whole run to the sequential path instead of failing it — the
    slow way home beats no way home.
    """
    pool = None
    if workers is None:
        workers = config.extract_workers
    auto = workers == 0  # the heuristic decides; an explicit count is honoured
    if workers <= 0:
        workers = min(4, os.cpu_count() or 2)
    if workers != 1 and (not auto or _worth_a_pool(todo, getattr(embedder, "semantic", False))):
        pool = _open_pool(config, workers)
    if pool is None:
        for position, entry in todo:
            _store_one(position, entry, config, store, embedder, visual, stats, progress,
                       len_todo=len(todo), done_before=done_before, commit_every=commit_every)
        return

    futures = {}
    for position, entry in todo:
        job = _extract_image_job if entry.kind == "image" else _extract_text_job
        futures[pool.submit(job, entry, config)] = (position, entry)
    done = done_before
    try:
        for future in as_completed(futures):
            position, entry = futures[future]
            done += 1
            try:
                status = _store_prepared(entry, future.result(), store, stats)
            except Exception as exc:  # one broken file must not stop the run
                stats.failed += 1
                stats.errors.append((entry.path, f"{type(exc).__name__}: {exc}"))
                status = "error"
            if progress:
                progress(done, done_before + len(todo), status, entry.path)
            if done % commit_every == 0:
                store.commit()
    except BrokenExecutor:
        stats.errors.append(("", "worker pool died; finishing sequentially"))
        pool.shutdown(cancel_futures=True)
        remaining = [(position, entry) for future, (position, entry) in futures.items()
                     if not future.done()]
        for position, entry in remaining:
            _store_one(position, entry, config, store, embedder, visual, stats, progress,
                       len_todo=len(todo), done_before=done_before, commit_every=commit_every)
        return
    pool.shutdown()


def _store_one(position, entry, config, store, embedder, visual, stats, progress,
               len_todo: int, done_before: int, commit_every: int) -> None:
    """The sequential path: one process does everything for one file."""
    done = done_before + position
    try:
        if entry.kind == "image":
            status = _index_image(entry, config, store, embedder, visual, stats)
        else:
            status = _index_one(entry, config, store, embedder, visual, stats)
    except Exception as exc:
        stats.failed += 1
        stats.errors.append((entry.path, f"{type(exc).__name__}: {exc}"))
        status = "error"
    if progress:
        progress(done, done_before + len_todo, status, entry.path)
    if position % commit_every == 0:
        store.commit()


def _store_prepared(entry, prepared: dict, store: Store, stats: IndexStats) -> str:
    """Write one worker's payload, return the progress status. The main
    process trusts nothing here except its own store: the same-content skip is
    re-checked against the digest."""
    status = prepared.get("status", "index")
    if status == "undecodable":
        stats.skipped += 1
        return status
    digest = prepared["digest"]
    existing = store.get_file(entry.path)
    if existing and existing["sha256"] == digest:
        store.upsert_file(entry.path, entry.size, entry.mtime, digest, entry.kind)
        stats.skipped += 1
        return "same-content"
    chunks = prepared.get("chunks") or []
    vectors = prepared.get("vectors")
    vector_matrix = (
        np.frombuffer(vectors, dtype=np.float32).reshape(len(chunks), -1) if vectors else None
    )
    file_id = store.upsert_file(entry.path, entry.size, entry.mtime, digest, entry.kind)
    stats.chunks += store.replace_chunks(file_id, chunks, vector_matrix)
    symbols = prepared.get("symbols")
    if symbols:
        stats.symbols += store.replace_symbols(file_id, symbols)
    if entry.kind == "image":
        # The worker cannot know about copies: the digest check is a store
        # question, so the main process asks it here, after the payload lands.
        duplicate = store.image_by_sha(digest)
        if duplicate is not None and duplicate["path"] != entry.path:
            stats.duplicates += 1
            stats.skipped += 1
            return "duplicate"
        visual_vector = (
            np.frombuffer(prepared["visual"], dtype=np.float32)
            if prepared.get("visual") is not None
            else None
        )
        store.upsert_image(
            entry.path, file_id, digest, prepared["width"], prepared["height"],
            entry.size, entry.mtime, prepared.get("thumb"),
            prepared.get("ocr_text") or None, prepared.get("engine"),
            visual_vector, phash=prepared.get("phash"),
        )
        stats.images += 1
        status = "index-image"
        if not (prepared.get("ocr_text") or "").strip():
            stats.ocr_empty += 1
    stats.indexed += 1
    return status


# ------------------------------------------------------------------- workers

_WORKER_CONFIG: IndexConfig | None = None
_WORKER_EMBEDDER = None
_WORKER_VISUAL = None


def _worker_init(config: IndexConfig) -> None:
    global _WORKER_CONFIG
    _WORKER_CONFIG = config
    reset_fallbacks()


def _worker_embedder():
    global _WORKER_EMBEDDER
    if _WORKER_EMBEDDER is None:
        _WORKER_EMBEDDER = get_embedder("auto", _WORKER_CONFIG.model_dir)
    return _WORKER_EMBEDDER


def _worker_visual():
    global _WORKER_VISUAL
    if _WORKER_VISUAL is None:
        _WORKER_VISUAL = get_visual_embedder(_WORKER_CONFIG.model_dir)
    return _WORKER_VISUAL


def _open_pool(config: IndexConfig, workers: int | None):
    """A process pool, or None when it cannot exist here.

    Spawn is the only choice on Windows and the safest one everywhere: the
    workers re-import this module, and the lazily-created embedder keeps each
    of them from paying for a model until a job actually needs one.
    """
    try:
        import multiprocessing

        count = workers if workers and workers > 0 else min(4, os.cpu_count() or 2)
        context = multiprocessing.get_context("spawn")
        return ProcessPoolExecutor(
            max_workers=count, mp_context=context,
            initializer=_worker_init, initargs=(config,),
        )
    except Exception:
        return None


def _extract_text_job(entry, config: IndexConfig) -> dict:
    """Worker side of a document: read, extract, chunk, embed. Everything that
    costs seconds happens here; the main process only writes."""
    digest = sha256_file(entry.path)
    text = extract_text(entry.path, entry.kind, config.max_file_bytes)
    if not text.strip():
        return {"digest": digest, "status": "no-text"}
    chunks = chunk_text(text, config.chunk_chars, config.chunk_overlap)
    vectors = _worker_embedder().encode_passages(chunks)
    prepared = {
        "digest": digest,
        "chunks": chunks,
        "vectors": vectors.astype(np.float32).tobytes() if vectors is not None else None,
    }
    if entry.kind == "code":
        prepared["symbols"] = [s.as_tuple() for s in extract_for(entry.path, text)]
    return prepared


def _extract_image_job(entry, config: IndexConfig) -> dict:
    """Worker side of a screenshot: decode, OCR, thumbnail, hash, embed."""
    image = load_image(entry.path)
    if image is None:
        return {"status": "undecodable", "digest": None}
    digest = sha256_file(entry.path)

    ocr_text = ""
    engine_name = None
    if config.ocr_enabled:
        try:
            result = ocr_image(
                entry.path, config.ocr_languages, config.ocr_engine,
                cjk=config.ocr_cjk,
                cjk_model=config.ocr_cjk_model,
                cjk_keys=config.ocr_cjk_keys,
            )
            ocr_text = result.text
            engine_name = result.engine
        except OcrUnavailable:
            ocr_text = ""

    chunks = chunk_text(ocr_text, config.chunk_chars, config.chunk_overlap) if ocr_text.strip() else []
    vectors = _worker_embedder().encode_passages(chunks) if chunks else None
    visual = _worker_visual()
    vector = visual.encode_image(image) if visual is not None else visual_features(image)
    return {
        "digest": digest,
        "chunks": chunks,
        "vectors": vectors.astype(np.float32).tobytes() if vectors is not None else None,
        "ocr_text": ocr_text,
        "engine": engine_name,
        "width": image.size[0],
        "height": image.size[1],
        "thumb": thumbnail_bytes(image, config.thumb_size),
        "phash": dhash(image),
        "visual": np.asarray(vector, dtype=np.float32).tobytes() if vector is not None else None,
    }


def backfill_phash(store: Store, limit: int = 100_000) -> int:
    """Give a perceptual hash to images indexed before schema v4.

    Only decodes pixels — no OCR, no embeddings, no thumbnail — so it costs a
    few milliseconds per screenshot instead of a full re-index. Files that have
    since disappeared are left alone rather than marked with a bogus hash.
    """
    done = 0
    for row in store.missing_phash(limit):
        path = str(row["path"])
        try:
            image = load_image(path)
        except Exception:
            continue
        if image is None:
            continue
        store.set_phash(int(row["id"]), dhash(image))
        done += 1
    return done


def backfill_symbols(store: Store, limit: int = 100_000) -> int:
    """Extract definitions from code files indexed before `symbols` existed.

    Reads the files again rather than reusing chunks: a definition split across
    a chunk boundary is still a definition, and the whole pass is one regex per
    line over a corpus that is mostly text. Runs once — the flag in `meta` is
    what stops an empty result from being recomputed on every index run.
    """
    if store.get_meta("symbols_backfilled"):
        return 0
    done = 0
    for row in store.files_without_symbols(limit):
        path = str(row["path"])
        try:
            text = Path(path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found = extract_for(path, text)
        if found:
            done += store.replace_symbols(int(row["id"]), [s.as_tuple() for s in found])
    store.set_meta("symbols_backfilled", "1")
    return done


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
    if entry.kind == "code":
        # Definitions come from the text that was just extracted, so this costs
        # one regex pass over a file that has already been read and chunked.
        stats.symbols += store.replace_symbols(file_id, [s.as_tuple() for s in extract_for(entry.path, text)])
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
            result = ocr_image(
                entry.path, config.ocr_languages, config.ocr_engine,
                cjk=config.ocr_cjk,
                cjk_model=config.ocr_cjk_model,
                cjk_keys=config.ocr_cjk_keys,
            )
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
        thumb, ocr_text or None, engine_name, vector, phash=dhash(image),
    )
    stats.images += 1
    stats.indexed += 1
    return "index-image"
