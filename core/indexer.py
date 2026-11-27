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

import hashlib
import os
import time
from concurrent.futures import (
    BrokenExecutor,
    ProcessPoolExecutor,
    ThreadPoolExecutor,
    as_completed,
)
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from . import filevec
from .chunker import chunk_spans, normalize
from . import lifespan
from .config import IndexConfig
from .embed import get_embedder, get_visual_embedder
from .extract import extract_text, PLAIN_KINDS
from .extract.plain import extract_plain_bytes
from .extract.dates import find_doc_date
from .group import dhash
from .images import load_image, thumbnail_bytes, visual_features
from .ocr import OcrUnavailable, ocr_image, reset_fallbacks
from .scanner import dedupe_roots, iter_entries, sha256_file
from .store import Store, _pack_text
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
    doc_dates: int = 0
    sensitive: int = 0
    sensitive_examples: list[tuple[str, str]] = field(default_factory=list)
    seconds: float = 0.0
    # Per-stage wall time: scan/decide/store measured in the main process,
    # sha/extract/chunk/embed/ocr/visual summed from each worker's own
    # stopwatch. This is how a slow run gets argued about with numbers
    # instead of vibes.
    timings: dict[str, float] = field(default_factory=dict)
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
            "doc_dates": self.doc_dates,
            "sensitive": self.sensitive,
            "seconds": round(self.seconds, 2),
            "files_per_second": round(self.files_per_second, 1),
            "timings": {k: round(v, 2) for k, v in sorted(self.timings.items()) if v > 0.01},
        }


def _apply_nice() -> None:
    """AUTOFIND_NICE=1 lowers this process to below-normal priority: the
    indexer keeps working while the foreground app stays responsive."""
    if os.environ.get("AUTOFIND_NICE") != "1":
        return
    try:
        import ctypes
        # BELOW_NORMAL_PRIORITY_CLASS on Windows, nice on POSIX.
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetPriorityClass(handle, 0x00004000)
    except Exception:
        try:
            os.nice(5)
        except Exception:
            pass


def run_index(
    roots: Sequence[str | Path],
    config: IndexConfig,
    store: Store,
    embedder,
    visual=None,
    prune: bool = False,
    progress: Progress | None = None,
    commit_every: int = 1000,
    workers: int | None = None,
) -> IndexStats:
    _apply_nice()
    started = time.perf_counter()
    # The root list can carry repeats — the same folder twice, or a folder and
    # its subfolder side by side — and every caller feeds it straight from user
    # input. Cleaning it once here is the guarantee every entry point shares.
    roots = dedupe_roots(roots)
    # A folder of photographs teaches the OCR fallback chain nothing; a folder of
    # screenshots teaches it everything. Start each run with a clean slate so the
    # verdict from the previous folder does not carry over.
    reset_fallbacks()
    # Upgrades and killed downloads leave dead weight (old import backups,
    # .part staging, updater temp dirs of dead processes) in the data
    # directory. An index run already walks everything, so the same pass
    # cleans up quietly first; failures are non-fatal by design.
    try:
        from .maintenance import cleanup_stale_artifacts

        cleanup_stale_artifacts(config.home)
    except Exception:
        pass  # maintenance must never block indexing
    blocked: list[tuple[str, str]] = []
    scan_started = time.perf_counter()
    # The walk reports itself as a scan phase with no total: no percent can
    # be honest before the corpus size is known, so the UI shows found
    # files instead of a bar racing to 100%.
    entries: list = []
    for entry in iter_entries(
        roots,
        excluded_dirs=config.excluded_dirs,
        max_bytes=config.max_file_bytes,
        min_bytes=config.min_file_bytes,
        privacy=config.privacy and not config.allow_sensitive,
        skipped=blocked,
        categories=config.privacy_categories,
    ):
        entries.append(entry)
        if progress and len(entries) % 250 == 0:
            progress(len(entries), 0, "scan", entry.path)
    if progress:
        progress(len(entries), 0, "scan", "")
    stats = IndexStats(scanned=len(entries), sensitive=len(blocked), sensitive_examples=blocked[:5])
    stats.timings["scan"] = time.perf_counter() - scan_started
    seen: set[str] = set()

    # The cheap skip (size+mtime unchanged) needs the store, so it stays in the
    # main process and prunes the pool's workload before any process pays for a
    # spawn.
    decide_started = time.perf_counter()
    todo = []
    seen_sha: dict[str, str] = {}  # digest -> path dispatched earlier in this run
    for position, entry in enumerate(entries, start=1):
        seen.add(entry.path)
        was_new = False
        try:
            existing = store.get_file(entry.path)
            was_new = existing is None
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
                # would run first costs hundreds of milliseconds per copy. Its
                # file row is kept so the duplicates report can surface it; the
                # in-memory map catches copies inside this same run.
                digest = sha256_file(entry.path)
                duplicate = store.image_by_sha(digest)
                if duplicate is not None and duplicate["path"] != entry.path:
                    # Keep the copy's file row so the duplicates report can find
                    # and delete it; it gets no image row, OCR or chunks.
                    store.upsert_file(entry.path, entry.size, entry.mtime, digest, entry.kind)
                    stats.duplicates += 1
                    stats.skipped += 1
                    if progress:
                        progress(position, len(entries), "duplicate", entry.path)
                    continue
                first = seen_sha.get(digest)
                if first is not None and first != entry.path:
                    store.upsert_file(entry.path, entry.size, entry.mtime, digest, entry.kind)
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
        # Marked only here: the duplicate branches above `continue` before
        # reaching this point, so their upserts keep the normal update path.
        if was_new:
            store.mark_new(entry.path)
        todo.append((position, entry))

    stats.timings["decide"] = time.perf_counter() - decide_started
    # Light files first: a cold run becomes searchable in the first minutes,
    # while the PDFs and screenshots are still grinding through extraction
    # and OCR. Positions renumber so the progress bar keeps climbing.
    todo.sort(key=lambda item: (item[1].kind in HEAVY_KINDS, item[1].size))
    # 1-based positions feed progress directly in the sequential path; a
    # 0-based renumber made the bar stop one file short of the total.
    todo = [(number, entry) for number, (_, entry) in enumerate(todo, start=1)]
    done_count = len(entries) - len(todo)
    extract_started = time.perf_counter()
    _run_extraction(todo, config, store, embedder, visual, stats, progress, commit_every, workers, done_count)
    stats.timings["extract_wall"] = time.perf_counter() - extract_started

    finalize_started = time.perf_counter()
    if progress:
        # every file is processed; what remains is commit, backfills and the
        # sealed sidecar - the bar must hold below 100% through it all
        progress(len(entries), len(entries), "finalize", "")
    if prune:
        stats.pruned = store.prune(seen)
    store.commit()
    # Old indexes predate `images.phash`, and an unchanged file is never
    # re-indexed, so without this the burst detector silently sees no hashes on
    # exactly the screenshots that were there first. Cheap: decode only, no OCR.
    stats.rehashed = backfill_phash(store)
    # Same problem, same shape: a code file indexed before the symbols table
    # existed is never re-indexed, so `sym:` would find nothing in exactly the
    # files that were there first. One pass, guarded by a meta flag.
    stats.symbols += backfill_symbols(store)
    # Document dates work the same way: files indexed before `doc_date` existed
    # carry NULL until one pass reads their first chunks and tries the regexes.
    stats.doc_dates = backfill_doc_dates(store)
    store.commit()
    # Nothing to prebuild for vectors: one packed row per file unpacks in a few
    # milliseconds on the first query, and the resident reader keeps it warm.
    if store.sealed:
        # Paranoid mode: FTS was dropped from disk, so the in-RAM BM25 sidecar
        # is the only content index - re-encrypt it with the fresh corpus. The
        # sidecar is a cache, not index truth: a live reader that blocks the
        # rewrite is recorded, and the next cold load discards the stale file
        # by fingerprint.
        try:
            store.reseal_bm25()
        except Exception as exc:
            stats.errors.append(("bm25seal", f"{type(exc).__name__}: {exc}"))
    stats.timings["finalize"] = time.perf_counter() - finalize_started
    stats.seconds = time.perf_counter() - started
    return stats


HEAVY_KINDS = frozenset({"image", "pdf", "docx", "pptx", "xlsx", "epub", "odt"})


def _extract_batch_job(jobs, config: IndexConfig) -> list[dict]:
    """Worker side of a batch of light files: one pickle round-trip instead of
    one per file. Returns results in the same order as the jobs it received,
    so the main process can zip them back with their positions."""
    job = _extract_image_job
    return [
        job(entry, config) if entry.kind == "image" else _extract_text_job(entry, config)
        for _, entry in jobs
    ]


def _worth_a_pool(todo: Sequence[tuple[int, object]], neural: bool) -> bool:
    """Is there enough expensive work to pay for spawning the workers?

    Each Windows spawn re-imports numpy and PIL — a few hundred milliseconds
    per worker, once. A hundred plain-text files extract in less time than
    that; four PDFs or screenshots already cost seconds each, so a handful of
    heavy files tips the balance long before a pile of light ones does. A
    neural embedder counts as heavy work too: its forward pass moves into the
    workers with the text. Volume counts too: a thousand light files in one
    process is a hundred-fold serial stretch of work that parallelises for
    free, so big runs take the pool even when every file is cheap.
    """
    heavy = sum(1 for _, entry in todo if entry.kind in HEAVY_KINDS)
    return heavy >= 4 or len(todo) >= 1000 or len(todo) >= 40 and neural


def _default_workers(todo: Sequence[tuple[int, object]]) -> int:
    """Pool size from the workload, not from a constant.

    rapidocr wants roughly 300 MB and its own model set per instance, so extra
    workers only pay off when there is enough heavy work for them: a run of
    plain text files stays at the lean default, while a screenshot-heavy run
    takes the extra cores this machine has.
    """
    heavy = sum(1 for _, entry in todo if entry.kind in HEAVY_KINDS)
    # Measurement on a 10k plain-text corpus (native read path): 6 workers was
    # fastest, 8 and 12 lost to process startup plus memory bandwidth. More
    # cores still pay off when the queue is genuinely heavy (OCR/PDF), where a
    # single future takes seconds and overlaps the store writes.
    if heavy >= 16:
        cap = 12
    elif len(todo) >= 1000:
        cap = 6
    else:
        cap = 4
    return min(cap, os.cpu_count() or 2)


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
        workers = _default_workers(todo)
    if workers != 1 and (not auto or _worth_a_pool(todo, getattr(embedder, "semantic", False))):
        pool = _open_pool(config, workers, stats)
    if pool is None:
        for position, entry in todo:
            _store_one(position, entry, config, store, embedder, visual, stats, progress,
                       len_todo=len(todo), done_before=done_before, commit_every=commit_every)
        return

    # Light files travel in batches so the pickle round-trip (text + vectors
    # per file) amortises over dozens of files; heavy files stay one per
    # future so progress stays live while a single screenshot takes seconds.
    futures: dict = {}
    batch: list[tuple[int, object]] = []

    def _flush_batch() -> None:
        nonlocal batch
        if batch:
            jobs = batch
            futures[pool.submit(_extract_batch_job, jobs, config)] = jobs
            batch = []

    for position, entry in todo:
        if entry.kind in HEAVY_KINDS or entry.kind == "image":
            _flush_batch()
            futures[pool.submit(_extract_image_job, entry, config)] = [(position, entry)]
        else:
            batch.append((position, entry))
            if len(batch) >= 64:
                _flush_batch()
    _flush_batch()
    done = done_before
    processed: set[int] = set()  # job keys already stored, so a broken pool
    # cannot reprocess them and report done > total
    try:
        for future in as_completed(futures):
            jobs = futures[future]
            try:
                prepared_batch = future.result()
            except BrokenExecutor:
                raise  # handled below: demote the whole run to sequential
            except Exception as exc:
                # A parser/embedder exception in a worker used to escape here and
                # abort the run before the final commit. Charge the files to the
                # stats and keep the remaining futures moving.
                for position, entry in jobs:
                    stats.failed += 1
                    stats.errors.append((entry.path, f"worker {type(exc).__name__}: {exc}"))
                    processed.add(id(entry))
                    store.end_new(entry.path)
                    done += 1
                    if progress:
                        progress(done, done_before + len(todo), "error", entry.path)
                continue
            if isinstance(prepared_batch, dict):
                prepared_batch = [prepared_batch]  # single-heavy-file future
            pairs = list(zip(jobs, prepared_batch, strict=False))
            if len(prepared_batch) < len(jobs):
                # A worker returning a short batch used to silently drop files
                # while progress still claimed them as done. Re-run the missing
                # entries here so the corpus stays complete.
                short = len(jobs) - len(prepared_batch)
                stats.errors.append((
                    "", f"extractor returned {len(prepared_batch)} results for {len(jobs)} files; "
                        f"retrying {short} in-process",
                ))
                for position, entry in jobs[len(prepared_batch):]:
                    try:
                        if entry.kind == "image":
                            retry = _extract_image_job(entry, config)
                        else:
                            retry = _extract_text_job(entry, config)
                        if isinstance(retry, dict):
                            retry = [retry]
                        if not retry:
                            raise RuntimeError("retry produced no result")
                        pairs.append(((position, entry), retry[0]))
                    except Exception as exc:
                        stats.failed += 1
                        stats.errors.append((entry.path, f"retry {type(exc).__name__}: {exc}"))
                        pairs.append(((position, entry), {"error": str(exc)}))
            for (position, entry), prepared in pairs:
                done += 1
                processed.add(id(entry))
                try:
                    mark = time.perf_counter()
                    # Each worker reports its own stopwatch per file: sha, extract,
                    # chunk, embed, ocr — summed here, so a slow run answers "which
                    # stage ate the time" without a profiler attached.
                    for key, value in (prepared.get("timing") or {}).items():
                        stats.timings[key] = stats.timings.get(key, 0.0) + float(value)
                    status = _store_prepared(entry, prepared, store, stats)
                    stats.timings["store"] = stats.timings.get("store", 0.0) + (
                        time.perf_counter() - mark
                    )
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
        remaining = [
            item for jobs in futures.values() for item in jobs
            if id(item[1]) not in processed
        ]
        for position, entry in remaining:
            _store_one(position, entry, config, store, embedder, visual, stats, progress,
                       len_todo=len(todo), done_before=done_before, commit_every=commit_every)
        return
    pool.shutdown()


def index_single(store, embedder, path, config) -> int:
    """Re-read one file in this process and rewrite its rows. Returns chunk count."""
    from .scanner import iter_entries

    target = os.path.normcase(str(path))
    entries = [
        entry
        for entry in iter_entries(
            [Path(path)],
            excluded_dirs=frozenset(),
            max_bytes=config.max_file_bytes,
            min_bytes=config.min_file_bytes,
            privacy=config.privacy and not config.allow_sensitive,
            categories=config.privacy_categories,
        )
        if os.path.normcase(entry.path) == target
    ]
    if not entries:
        return 0
    entry = entries[0]
    stats = IndexStats()
    visual = None
    row = store.get_file(str(path))
    if row is not None:
        store.drop_file(int(row["id"]))
    _store_one(0, entry, config, store, embedder, visual, stats,
               progress=None, len_todo=1, done_before=0, commit_every=1)
    store.commit()
    return stats.chunks


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
    finally:
        # The sequential path does not pass `fresh` into replace_chunks, but
        # the marker must not outlive the file (or the next run's path check).
        store.end_new(entry.path)
    if progress:
        progress(done, done_before + len_todo, status, entry.path)
    if position % commit_every == 0:
        store.commit()


def _store_prepared(entry, prepared: dict, store: Store, stats: IndexStats) -> str:
    """Write one worker's payload, return the progress status. The main
    process trusts nothing here except its own store: the same-content skip is
    re-checked against the digest."""
    status = prepared.get("status", "index")
    # Consume the marker at the top: every early return below (name-only,
    # undecodable, duplicate, same-content) must leave the writer clean.
    fresh = store.is_new(entry.path)
    store.end_new(entry.path)
    if status == "name-only":
        store.upsert_file(entry.path, entry.size, entry.mtime, "", "other")
        stats.indexed += 1
        return status
    if status == "undecodable":
        stats.skipped += 1
        return status
    if status == "same-content":
        # The worker saw this exact path already indexed (touched, bytes
        # unchanged) and skipped the expensive image work. Refresh the row and
        # leave the rest of the payload absent — it has no width/height/text.
        store.upsert_file(entry.path, entry.size, entry.mtime, prepared["digest"], entry.kind)
        stats.skipped += 1
        return "same-content"
    if status == "seen-image":
        # A pixel-identical copy of something already indexed: refresh this
        # path's file row, count the copy, and write nothing else — no OCR,
        # no chunks, no thumbnail. The twin carries the search.
        store.upsert_file(entry.path, entry.size, entry.mtime, prepared["digest"], entry.kind)
        stats.skipped += 1
        stats.duplicates += 1
        return "duplicate"
    digest = prepared["digest"]
    if not fresh:
        existing = store.get_file(entry.path)
        if existing and existing["sha256"] == digest:
            store.upsert_file(entry.path, entry.size, entry.mtime, digest, entry.kind)
            stats.skipped += 1
            return "same-content"
    chunks = prepared.get("text") or ""
    spans = prepared.get("spans") or []
    vector = (
        np.frombuffer(prepared["vector"], dtype=np.float32)
        if prepared.get("vector") is not None
        else None
    )
    file_id = store.upsert_file(
        entry.path, entry.size, entry.mtime, digest, entry.kind,
        doc_date=prepared.get("doc_date"),
    )
    stats.chunks += store.replace_chunks(
        file_id, chunks, spans, vector, prepared.get("packed"), fresh=fresh
    )
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
            vector=visual_vector, phash=prepared.get("phash") or 0,
            ocr_boxes=prepared.get("ocr_boxes"),
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


def _open_pool(config: IndexConfig, workers: int | None, stats: IndexStats | None = None):
    """A process pool, or None when it cannot exist here.

    Spawn is the only choice on Windows and the safest one everywhere: the
    workers re-import this module, and the lazily-created embedder keeps each
    of them from paying for a model until a job actually needs one.
    """
    try:
        import multiprocessing

        count = workers if workers and workers > 0 else min(4, os.cpu_count() or 2)
        # With the native reader the file read runs without the GIL, so threads
        # overlap I/O without paying for spawn, pickling or a second copy of
        # every decoded document. AUTOFIND_NO_THREADS=1 forces processes (the
        # documented switch); AUTOFIND_THREAD_POOL=1 is the legacy opt-in that
        # actually turns threads on, and is still honoured.
        force_processes = os.environ.get("AUTOFIND_NO_THREADS") == "1"
        want_threads = os.environ.get("AUTOFIND_THREAD_POOL") == "1"
        if want_threads and not force_processes and _native_text() is not None:
            return ThreadPoolExecutor(
                max_workers=count,
                initializer=_worker_init, initargs=(config,),
            )
        context = multiprocessing.get_context("spawn")
        return ProcessPoolExecutor(
            max_workers=count, mp_context=context,
            initializer=_worker_init, initargs=(config,),
        )
    except Exception as exc:
        if stats is not None:
            stats.errors.append(("", f"pool unavailable: {type(exc).__name__}: {exc}"))
        return None


_NATIVE = None
_NATIVE_READ = None


def _native_text():
    """The optional afnative CPython extension, cached per process.

    Built by `python native/build_native.py`. Missing/old .pyd or
    AUTOFIND_NO_NATIVE=1 returns None, and the pure-Python read/hash/decode
    path below stays authoritative (also used for A/B benchmarks).

    On Windows the extension exposes a CreateFileW/ReadFile reader; that is
    the default when present. AUTOFIND_NATIVE_CRT=1 selects the older CRT
    reader for A/B measurements.
    """
    global _NATIVE, _NATIVE_READ
    if _NATIVE is None:
        if os.environ.get("AUTOFIND_NO_NATIVE"):
            _NATIVE = False
            return None
        try:
            import afnative

            _NATIVE = afnative
        except Exception:
            _NATIVE = False
    if not _NATIVE:
        return None
    if _NATIVE_READ is None:
        win32 = getattr(_NATIVE, "read_text_digest_win32", None)
        _NATIVE_READ = (
            win32 if win32 is not None and not os.environ.get("AUTOFIND_NATIVE_CRT")
            else _NATIVE.read_text_digest
        )
    return _NATIVE


def _extract_text_job(entry, config: IndexConfig) -> dict:
    """Worker side of a document: read, extract, chunk, embed. Everything that
    costs seconds happens here; the main process only writes.

    Plain text and code — the bulk of any corpus — are read ONCE: the digest
    and the text both come out of the same bytes. A file used to pay two
    open/read round-trips (one for the hash, one for extraction), which at
    small file sizes is nearly all the per-file cost."""
    timing: dict[str, float] = {}
    mark = time.perf_counter()
    if entry.kind == "other":
        # Name-only entry: no read, no hash, no chunks. The filename is the
        # searchable surface, which is all a binary or unknown format offers.
        return {"status": "name-only", "digest": "", "timing": timing}
    if entry.kind in PLAIN_KINDS and not str(entry.path).lower().endswith((".csv", ".tsv")):
        # csv/tsv are "text" to the store, but they read better row-shaped,
        # so they go through extract_text (see core/extract/__init__.py).
        # Read 1 MB first: read(n) preallocates n bytes, and a 20 MB buffer per
        # 200-byte file costs more than the file does. Only oversized files
        # trigger the second read.
        cap = config.max_file_bytes
        native = _native_text()
        if native is not None:
            # One C call: wide-path CRT read, SHA-256 and the same encoding
            # ladder as extract_plain_bytes, without Python reader/encoder
            # object churn.
            text, digest = _NATIVE_READ(entry.path, cap)
            text = text or ""
        else:
            with open(entry.path, "rb") as handle:
                data = handle.read(min(cap, 1 << 20))
                if len(data) == min(cap, 1 << 20) and cap > (1 << 20):
                    data += handle.read(cap - len(data))
            digest = hashlib.sha256(data).digest()  # covers the indexed prefix
            text = extract_plain_bytes(data)
        timing["read"] = time.perf_counter() - mark
        mark = time.perf_counter()
    else:
        digest = sha256_file(entry.path)
        timing["sha"] = time.perf_counter() - mark
        mark = time.perf_counter()
        text = extract_text(entry.path, entry.kind, config.max_file_bytes, ocr=config.ocr_enabled)
    timing["extract"] = time.perf_counter() - mark
    if not text.strip():
        return {"digest": digest, "status": "no-text", "timing": timing}
    mark = time.perf_counter()
    # chunk_spans() normalises internally; slice the same normalised copy so
    # CRLF/leading space cannot shift a span away from its text.
    norm = normalize(text)
    spans = chunk_spans(norm, config.chunk_chars, config.chunk_overlap)
    chunks = [norm[start:end] for start, end in spans]
    timing["chunk"] = time.perf_counter() - mark
    mark = time.perf_counter()
    vectors = _worker_embedder().encode_passages(chunks)
    timing["embed"] = time.perf_counter() - mark
    mark = time.perf_counter()
    # Compressing the text and hashing it is CPU work the worker can do while
    # the main process is still writing an earlier file. Only the compressed
    # blocks cross the pipe, too, so the transfer shrinks.
    packed = _pack_text(norm)
    timing["pack"] = time.perf_counter() - mark
    prepared = {
        "digest": digest,
        # The document text travels once and the spans point into it, instead
        # of shipping a list of overlapping chunk strings that the writer then
        # has to store a second time.
        "text": norm,
        "spans": spans,
        "vector": (
            filevec.mean_pool(vectors).astype(np.float32).tobytes() if vectors is not None else None
        ),
        "packed": packed,
        "doc_date": find_doc_date(norm),
        "timing": timing,
    }
    if entry.kind == "code":
        prepared["symbols"] = [s.as_tuple() for s in extract_for(entry.path, norm)]
    return prepared


def _extract_image_job(entry, config: IndexConfig) -> dict:
    """Worker side of a screenshot: decode, OCR, thumbnail, hash, embed."""
    timing: dict[str, float] = {}
    mark = time.perf_counter()
    image = load_image(entry.path)
    if image is None:
        return {"status": "undecodable", "digest": None}
    timing["decode"] = time.perf_counter() - mark
    mark = time.perf_counter()
    digest = sha256_file(entry.path)
    timing["sha"] = time.perf_counter() - mark
    phash = dhash(image)

    # Same bytes or same exact pixels already indexed? OCR costs a real
    # inference per copy, and a re-saved screenshot decodes to identical
    # pixels — one lookup covers the whole family. A phash match alone is
    # not proof (near-duplicate screenshots share the gradient hash while
    # differing by a line of text), so the decoded frames are compared
    # before any OCR is skipped.
    mark = time.perf_counter()
    # The worker config is the argument passed into this job; _WORKER_CONFIG is
    # only initialised inside worker processes, so reading it here gave the
    # main process (sequential/retry path) a None db_path and always "no twin".
    twin = _worker_twin_image(config.db_path, digest, phash)
    timing["twin"] = time.perf_counter() - mark
    if twin is not None:
        if twin == entry.path:
            # Touched but byte-identical: the row refresh happens in the
            # main process, so spend no OCR on it here.
            return {"status": "same-content", "digest": digest, "phash": phash, "timing": timing}
        if _same_pixels(entry.path, twin):
            return {"status": "seen-image", "digest": digest, "phash": phash, "timing": timing}

    ocr_text = ""
    engine_name = None
    # Bound before the try: OcrUnavailable must not leave the dict-building
    # code below reading an unassigned name.
    ocr_boxes = None
    # A laptop on battery skips the OCR pass: minutes of CPU per
    # screenshot is not worth a drained battery. The name and the
    # visual vector still index; text comes with the next plugged run.
    if config.ocr_enabled and not lifespan.on_battery():
        mark = time.perf_counter()
        try:
            result = ocr_image(
                entry.path, config.ocr_languages, config.ocr_engine,
                cjk=config.ocr_cjk,
                cjk_model=config.ocr_cjk_model,
                cjk_keys=config.ocr_cjk_keys,
            )
            ocr_text = result.text
            engine_name = result.engine
            import json as _json
            ocr_boxes = _json.dumps(result.boxes, ensure_ascii=False) if result.boxes else None
        except OcrUnavailable:
            ocr_text = ""
        timing["ocr"] = time.perf_counter() - mark

    mark = time.perf_counter()
    norm = normalize(ocr_text)
    spans = chunk_spans(norm, config.chunk_chars, config.chunk_overlap) if norm.strip() else []
    chunks = [norm[start:end] for start, end in spans]
    vectors = _worker_embedder().encode_passages(chunks) if chunks else None
    timing["embed"] = time.perf_counter() - mark
    mark = time.perf_counter()
    visual = _worker_visual()
    vector = visual.encode_image(image) if visual is not None else visual_features(image)
    timing["visual"] = time.perf_counter() - mark
    return {
        "digest": digest,
        "text": norm,
        "spans": spans,
        "vector": (
            filevec.mean_pool(vectors).astype(np.float32).tobytes() if vectors is not None else None
        ),
        "ocr_text": ocr_text,
        "ocr_boxes": ocr_boxes if config.ocr_enabled else None,
        "engine": engine_name,
        "width": image.size[0],
        "height": image.size[1],
        "thumb": thumbnail_bytes(image, config.thumb_size),
        "phash": phash,
        "visual": np.asarray(vector, dtype=np.float32).tobytes() if vector is not None else None,
        "doc_date": find_doc_date(norm),
        "timing": timing,
    }


def _worker_twin_image(db_path: Path, digest: str, phash: int) -> str | None:
    """Path of an already-indexed image with the same bytes or the same pixels.

    A read-only connection straight to SQLite: the worker has no Store, and a
    two-column query is all it needs. A missing or locked database means "no
    shortcut", never a failure.
    """
    import sqlite3

    try:
        uri = f"file:{Path(db_path).as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
    except Exception:
        return None
    try:
        row = conn.execute(
            "SELECT path FROM images WHERE sha256 = ? OR phash = ? "
            "ORDER BY CASE WHEN sha256 = ? THEN 0 ELSE 1 END, id LIMIT 1",
            (digest, int(phash), digest),
        ).fetchone()
        return str(row[0]) if row else None
    except Exception:
        return None
    finally:
        conn.close()


def _same_pixels(first: str, second: str) -> bool:
    """True when two image files decode to exactly the same pixels.

    dhash is a gradient hash: near-duplicate screenshots collide on it while
    differing by a line of text, so a hash match is only trusted after the
    actual frames are compared. Runs once per candidate pair, not per file.
    """
    try:
        from PIL import Image, ImageChops

        with Image.open(first) as left, Image.open(second) as right:
            if left.size != right.size or left.mode != right.mode:
                return False
            return ImageChops.difference(left, right).getbbox() is None
    except Exception:
        return False


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


def backfill_doc_dates(store: Store, limit: int = 3000) -> int:
    """Give files indexed before `doc_date` existed the date from their chunks.

    One bounded pass per index run over files that still have NULL: the head
    chunks are already in the store, so this costs a handful of small queries
    and a regex per file, never a re-extract. The pass shrinks as files gain
    dates; files whose text simply has no date stay NULL and cost the lookup
    again next run — bounded by `limit`, so it stays cheap either way.
    """
    done = 0
    for file_id in store.files_without_doc_date(limit):
        try:
            doc = find_doc_date(store.chunk_heads(file_id))
        except Exception:
            continue
        if doc:
            store.set_doc_date(file_id, doc)
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
    if entry.kind == "other":
        store.upsert_file(entry.path, entry.size, entry.mtime, "", "other")
        stats.indexed += 1
        return "name-only"

    # Sequential indexing used to read every plain file twice: once for the
    # digest and once for the text. Use the same one-read native path as the
    # pool workers, so --workers 1 (and a dead pool) gets the same win.
    text = None
    if entry.kind in PLAIN_KINDS and not str(entry.path).lower().endswith((".csv", ".tsv")):
        cap = config.max_file_bytes
        native = _native_text()
        if native is not None:
            text, digest = _NATIVE_READ(entry.path, cap)
            text = text or ""
        else:
            with open(entry.path, "rb") as handle:
                data = handle.read(min(cap, 1 << 20))
                if len(data) == min(cap, 1 << 20) and cap > (1 << 20):
                    data += handle.read(cap - len(data))
            digest = hashlib.sha256(data).digest()
            text = extract_plain_bytes(data)
    else:
        digest = sha256_file(entry.path)

    existing = store.get_file(entry.path)
    if existing and existing["sha256"] == digest:
        store.upsert_file(entry.path, entry.size, entry.mtime, digest, entry.kind)
        stats.skipped += 1
        return "same-content"

    if text is None:
        text = extract_text(entry.path, entry.kind, config.max_file_bytes, ocr=config.ocr_enabled)
    if not text.strip():
        stats.skipped += 1
        return "no-text"

    norm = normalize(text)
    spans = chunk_spans(norm, config.chunk_chars, config.chunk_overlap)
    chunks = [norm[start:end] for start, end in spans]
    vectors = embedder.encode_passages(chunks)
    file_id = store.upsert_file(
        entry.path, entry.size, entry.mtime, digest, entry.kind,
        doc_date=find_doc_date(norm),
    )
    stats.chunks += store.replace_chunks(file_id, norm, spans, vectors)
    if entry.kind == "code":
        # Definitions come from the text that was just extracted, so this costs
        # one regex pass over a file that has already been read and chunked.
        stats.symbols += store.replace_symbols(file_id, [s.as_tuple() for s in extract_for(entry.path, norm)])
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
        # Keep the copy's file row so name search and the duplicates report
        # still see it — the twin carries the OCR and the image row.
        store.upsert_file(entry.path, entry.size, entry.mtime, digest, "image")
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

    norm = normalize(ocr_text)
    spans = chunk_spans(norm, config.chunk_chars, config.chunk_overlap) if norm.strip() else []
    chunks = [norm[start:end] for start, end in spans]
    vectors = embedder.encode_passages(chunks) if chunks else None
    stats.chunks += store.replace_chunks(file_id, norm, spans, vectors)
    doc = find_doc_date(norm) if norm.strip() else None
    if doc:
        store.set_doc_date(file_id, doc)

    thumb = thumbnail_bytes(image, config.thumb_size)
    vector = visual.encode_image(image) if visual is not None else visual_features(image)
    store.upsert_image(
        entry.path, file_id, digest, width, height, entry.size, entry.mtime,
        thumb, ocr_text or None, engine_name, vector, phash=dhash(image),
    )
    stats.images += 1
    stats.indexed += 1
    return "index-image"
