"""Command line entry point: semfind index / search / serve / stats."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import IndexConfig
from .embed import get_embedder, get_visual_embedder
from .indexer import run_index
from .ocr import engine_report
from .search import hybrid_search, rank_paths
from .store import Store

BAR_WIDTH = 28


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="semfind", description="Local search over files and screenshots. Nothing leaves the machine."
    )
    parser.add_argument("--db", type=Path, help="path to index.db (default: <SEMFIND_HOME>/index.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    index_cmd = sub.add_parser("index", help="scan folders and build the index")
    index_cmd.add_argument("roots", nargs="+", type=Path, help="folders or files to index")
    index_cmd.add_argument("--prune", action="store_true", help="drop files that disappeared from disk")
    index_cmd.add_argument("--embedder", default="auto", choices=["auto", "e5", "hashing"])
    index_cmd.add_argument("--no-ocr", action="store_true", help="skip screenshot OCR")
    index_cmd.add_argument("--quiet", action="store_true")

    search_cmd = sub.add_parser("search", help="hybrid search over the index")
    search_cmd.add_argument("query", nargs="+", help="search query")
    search_cmd.add_argument("-n", "--limit", type=int, default=10)
    search_cmd.add_argument("--candidates", type=int, default=50)
    search_cmd.add_argument("--kind", default=None, help="text|code|pdf|docx|image")
    search_cmd.add_argument("--files", action="store_true", help="collapse results to one row per file")
    search_cmd.add_argument("--json", action="store_true", help="machine readable output")
    search_cmd.add_argument("--embedder", default="auto", choices=["auto", "e5", "hashing"])

    serve_cmd = sub.add_parser("serve", help="start the local web UI")
    serve_cmd.add_argument("--port", type=int, default=8765)
    serve_cmd.add_argument("--no-browser", action="store_true")

    sub.add_parser("stats", help="show index size and settings")

    args = parser.parse_args(argv)
    config = IndexConfig()
    db_path = args.db or config.db_path

    if args.command == "index":
        return _cmd_index(args, config, db_path)
    if args.command == "search":
        return _cmd_search(args, config, db_path)
    if args.command == "serve":
        from .api import serve

        serve(db_path, config, port=args.port, open_browser=not args.no_browser)
        return 0
    return _cmd_stats(config, db_path)


def _cmd_index(args, config: IndexConfig, db_path: Path) -> int:
    config.ensure_home()
    if args.no_ocr:
        config.ocr_enabled = False
    embedder = get_embedder(args.embedder, config.model_dir)
    visual = get_visual_embedder(config.model_dir)
    store = Store(db_path, embedder.name, embedder.dim)
    try:
        started = time.perf_counter()
        stats = run_index(
            args.roots,
            config,
            store,
            embedder,
            visual,
            prune=args.prune,
            progress=None if args.quiet else _progress,
        )
        elapsed = time.perf_counter() - started
    finally:
        store.close()

    print()
    print(f"text embedder  {embedder.name}")
    print(f"visual         {visual.name}")
    print(f"ocr            {engine_report(config.ocr_languages)['active'] or 'none'}")
    print(f"scanned        {stats.scanned}")
    print(f"indexed        {stats.indexed}")
    print(f"  of which images {stats.images}")
    print(f"skipped        {stats.skipped}")
    print(f"  duplicates   {stats.duplicates}")
    print(f"failed         {stats.failed}")
    print(f"chunks         {stats.chunks}")
    if stats.pruned:
        print(f"pruned         {stats.pruned}")
    print(f"time           {elapsed:.1f}s  ({stats.files_per_second:.1f} files/s)")
    for path, error in stats.errors[:5]:
        print(f"  ! {path}: {error}", file=sys.stderr)
    return 0


def _cmd_search(args, config: IndexConfig, db_path: Path) -> int:
    if not db_path.exists():
        print(f"no index at {db_path}; run: semfind index <folder>", file=sys.stderr)
        return 2
    embedder = get_embedder(args.embedder, config.model_dir)
    store = Store(db_path, embedder.name, embedder.dim, readonly=True)
    try:
        query = " ".join(args.query)
        started = time.perf_counter()
        hits = hybrid_search(
            store,
            embedder,
            query,
            limit=args.limit,
            candidates=args.candidates,
            kinds=(args.kind,) if args.kind else None,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        store.close()

    if args.json:
        payload = [
            {
                "path": hit.path,
                "kind": hit.kind,
                "chunk": hit.ord,
                "score": hit.score,
                "channels": list(hit.channels),
                "snippet": hit.snippet,
            }
            for hit in hits
        ]
        print(json.dumps({"query": query, "ms": round(elapsed_ms, 1), "hits": payload}, ensure_ascii=False, indent=2))
        return 0

    if not hits:
        print(f"nothing found for «{query}» ({elapsed_ms:.0f} ms)")
        return 1

    print(f"{len(hits)} results in {elapsed_ms:.0f} ms — «{query}»\n")
    if args.files:
        for position, (path, score) in enumerate(rank_paths(hits), start=1):
            print(f"{position:2}. {score:.4f}  {path}")
        return 0

    for position, hit in enumerate(hits, start=1):
        channels = "+".join(hit.channels)
        print(f"{position:2}. {hit.score:.4f}  [{channels}]  {hit.name}  ·  chunk {hit.ord}")
        print(f"    {hit.path}")
        print(f"    {hit.snippet}")
        print()
    return 0


def _cmd_stats(config: IndexConfig, db_path: Path) -> int:
    if not db_path.exists():
        print(f"no index at {db_path}")
        return 2
    embedder = get_embedder("auto", config.model_dir)
    visual = get_visual_embedder(config.model_dir)
    store = Store(db_path, embedder.name, embedder.dim, readonly=True)
    try:
        stats = store.stats()
    finally:
        store.close()
    print(f"db             {db_path}")
    print(f"text embedder  {embedder.name}")
    print(f"visual         {visual.name}")
    print(f"ocr            {engine_report(config.ocr_languages)['active'] or 'none'}")
    print(f"files          {stats['files']}")
    print(f"chunks         {stats['chunks']}")
    print(f"images         {stats['images']}  (с OCR-текстом: {stats['ocr_images']})")
    print(f"vectors        {stats['vectors']}")
    print(f"size           {stats['db_bytes'] / 1024 / 1024:.1f} MB")
    return 0


def _progress(position: int, total: int, status: str, path: str) -> None:
    if total == 0:
        return
    if sys.stderr.isatty():
        filled = int(BAR_WIDTH * position / total)
        bar = "█" * filled + "·" * (BAR_WIDTH - filled)
        print(f"\r  {bar} {position}/{total} {status:<12}", end="", file=sys.stderr, flush=True)
    elif position % 100 == 0 or position == total:
        print(f"  {position}/{total} {status}", file=sys.stderr, flush=True)


def _force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


if __name__ == "__main__":
    raise SystemExit(main())
