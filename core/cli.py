"""Command line entry point: semfind index / search / serve / stats."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from .config import IndexConfig
from .doctor import collect as collect_doctor
from .doctor import format_report as format_doctor
from .embed import get_embedder, get_visual_embedder
from .evaluate import compare_channels, evaluate, format_report, load_labels
from .indexer import run_index
from .ocr import engine_report
from .privacy import indexed_sensitive, purge_indexed
from .search import hybrid_search, rank_paths
from .store import Store
from .watch import Watcher, watch_forever

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
    index_cmd.add_argument(
        "--allow-sensitive", action="store_true", help="index password stores and key material too"
    )
    index_cmd.add_argument("--quiet", action="store_true")

    watch_cmd = sub.add_parser("watch", help="keep the index in sync with folders")
    watch_cmd.add_argument("roots", nargs="+", type=Path)
    watch_cmd.add_argument("--interval", type=float, default=2.0, help="seconds between passes")
    watch_cmd.add_argument("--settle", type=float, default=1.0, help="quiet seconds before a file is read")
    watch_cmd.add_argument("--allow-sensitive", action="store_true")

    eval_cmd = sub.add_parser("eval", help="measure Recall@k on a labelled query set")
    eval_cmd.add_argument("labels", type=Path, help="labels json")
    eval_cmd.add_argument("-n", "--limit", type=int, default=10)
    eval_cmd.add_argument("--candidates", type=int, default=200)
    eval_cmd.add_argument("--ablate", action="store_true", help="also score bm25 only and vector only")
    eval_cmd.add_argument("--verbose", action="store_true", help="print every query")
    eval_cmd.add_argument("--json", action="store_true")

    doctor_cmd = sub.add_parser("doctor", help="check the environment and the bundle budget")
    doctor_cmd.add_argument("--json", action="store_true")

    privacy_cmd = sub.add_parser("privacy", help="list or remove sensitive files already in the index")
    privacy_cmd.add_argument("action", nargs="?", default="scan", choices=["scan", "purge"])
    privacy_cmd.add_argument("--json", action="store_true")

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
    if args.command == "watch":
        return _cmd_watch(args, config, db_path)
    if args.command == "eval":
        return _cmd_eval(args, config, db_path)
    if args.command == "doctor":
        return _cmd_doctor(args, config, db_path)
    if args.command == "privacy":
        return _cmd_privacy(args, config, db_path)
    if args.command == "serve":
        from .api import serve

        serve(db_path, config, port=args.port, open_browser=not args.no_browser)
        return 0
    return _cmd_stats(config, db_path)


def _cmd_index(args, config: IndexConfig, db_path: Path) -> int:
    config.ensure_home()
    if args.no_ocr:
        config.ocr_enabled = False
    if getattr(args, "allow_sensitive", False):
        config.allow_sensitive = True
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
    if stats.sensitive:
        print(f"skipped private {stats.sensitive}")
        for path, reason in stats.sensitive_examples[:3]:
            print(f"  · {path}  ({reason})")
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


def _cmd_watch(args, config: IndexConfig, db_path: Path) -> int:
    config.ensure_home()
    if getattr(args, "allow_sensitive", False):
        config.allow_sensitive = True

    def report(changed: list[str], removed: list[str], result: dict) -> None:
        parts = []
        if result.get("indexed"):
            parts.append(f"{result['indexed']} updated")
        if result.get("removed"):
            parts.append(f"{result['removed']} removed")
        if result.get("failed"):
            parts.append(f"{result['failed']} failed")
        stamp = time.strftime("%H:%M:%S")
        print(f"  {stamp}  {', '.join(parts) or 'no change'}")
        for path in (changed[:2] + removed[:1]):
            print(f"          {path}")

    watcher = Watcher(
        args.roots,
        config,
        db_path,
        interval=args.interval,
        settle=args.settle,
        on_batch=report,
    )
    return watch_forever(watcher)


def _cmd_eval(args, config: IndexConfig, db_path: Path) -> int:
    if not db_path.exists():
        print(f"no index at {db_path}; run: semfind index <folder>", file=sys.stderr)
        return 2
    try:
        labels = load_labels(args.labels)
    except (OSError, ValueError) as exc:
        print(f"cannot read labels: {exc}", file=sys.stderr)
        return 2

    embedder = get_embedder("auto", config.model_dir)
    store = Store(db_path, embedder.name, embedder.dim, readonly=True)
    try:
        report = evaluate(store, embedder, labels, limit=args.limit, candidates=args.candidates)
        ablation = (
            compare_channels(store, embedder, labels, limit=args.limit, candidates=args.candidates)
            if args.ablate
            else {}
        )
    finally:
        store.close()

    if args.json:
        payload = report.as_dict()
        if ablation:
            payload["ablation"] = {name: value.as_dict() for name, value in ablation.items()}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(format_report(report, verbose=args.verbose))
    if ablation:
        print("\nablation")
        for name, value in ablation.items():
            print(
                f"  {name:<7} recall@{args.limit} {value.recall:.3f}   mrr {value.mrr:.3f}   "
                f"p95 {value.latency['p95']:.0f} ms"
            )
    return 0


def _cmd_doctor(args, config: IndexConfig, db_path: Path) -> int:
    data = collect_doctor(config, db_path)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(format_doctor(data))
    return 0


def _cmd_privacy(args, config: IndexConfig, db_path: Path) -> int:
    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    embedder = get_embedder("auto", config.model_dir)
    if args.action == "purge":
        store = Store(db_path, embedder.name, embedder.dim)
        try:
            result = purge_indexed(store)
        finally:
            store.close()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(f"removed {result['removed']} sensitive file(s) from the index")
            for path in result["files"]:
                print(f"  · {path}")
        return 0

    store = Store(db_path, embedder.name, embedder.dim, readonly=True)
    try:
        found = indexed_sensitive(store)
    finally:
        store.close()
    if args.json:
        print(json.dumps({"count": len(found), "files": found}, ensure_ascii=False, indent=2))
        return 0
    if not found:
        print("no sensitive files in the index")
        return 0
    print(f"{len(found)} file(s) the filter would skip today:")
    for item in found:
        print(f"  {item['reason']:<32} {item['path']}")
    print("\nremove them with: semfind privacy purge")
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
