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
from .query import understand
from .search import search_page
from .store import Store
from .watch import Watcher, watch_forever

BAR_WIDTH = 28


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="semfind", description="Local search over files and screenshots. Nothing leaves the machine."
    )
    parser.add_argument("--db", type=Path, help="path to index.db (default: <SEMFIND_HOME>/index.db)")
    parser.add_argument(
        "--vector-backend",
        default="pack",
        choices=["pack", "sqlite-vec", "blob"],
        help="how chunk vectors are stored and searched (default: pack)",
    )
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

    ocr_cmd = sub.add_parser("ocr", help="list readable languages, or fetch a script model")
    ocr_cmd.add_argument("action", nargs="?", default="languages",
                         choices=["languages", "download", "read"])
    ocr_cmd.add_argument("target", nargs="?", default=None,
                         help="language or model for `download`, image for `read`")
    ocr_cmd.add_argument("--lang", default=None,
                         help="pin one model when reading, e.g. korean, arabic, cyrillic")
    ocr_cmd.add_argument("--json", action="store_true")

    search_cmd = sub.add_parser("search", help="hybrid search over the index")
    search_cmd.add_argument("query", nargs="+", help="search query")
    search_cmd.add_argument("-n", "--limit", type=int, default=10)
    search_cmd.add_argument("--offset", type=int, default=0)
    search_cmd.add_argument("--candidates", type=int, default=50)
    search_cmd.add_argument("--kind", default=None, help="text|code|pdf|docx|image")
    search_cmd.add_argument("--sort", default=None, choices=["best", "newest", "oldest", "largest", "name"])
    search_cmd.add_argument("--files", action="store_true", help="collapse results to one row per file")
    search_cmd.add_argument("--json", action="store_true", help="machine readable output")
    search_cmd.add_argument("--embedder", default="auto", choices=["auto", "e5", "hashing"])

    serve_cmd = sub.add_parser("serve", help="start the local web UI in a browser")
    serve_cmd.add_argument("--port", type=int, default=8765)
    serve_cmd.add_argument("--no-browser", action="store_true")

    app_cmd = sub.add_parser("app", help="open the desktop window (tray + global hotkey)")
    app_cmd.add_argument("--port", type=int, default=8765)
    app_cmd.add_argument("--hotkey", default="ctrl+alt+space", help="show/hide key, e.g. ctrl+alt+space")
    app_cmd.add_argument("--no-hotkey", action="store_true")
    app_cmd.add_argument("--no-tray", action="store_true")
    app_cmd.add_argument("--hidden", action="store_true", help="start in the tray")
    app_cmd.add_argument("--windowed", action="store_true", help="framed window instead of borderless")
    app_cmd.add_argument("--browser", action="store_true", help="use the browser even if a window is possible")
    app_cmd.add_argument("--debug", action="store_true")

    autostart_cmd = sub.add_parser("autostart", help="start the app with Windows (tray, no console)")
    autostart_cmd.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"])
    autostart_cmd.add_argument("--json", action="store_true")

    shortcut_cmd = sub.add_parser("shortcut", help="create desktop / Start Menu shortcuts")
    shortcut_cmd.add_argument("action", nargs="?", default="create", choices=["create", "remove"])
    shortcut_cmd.add_argument("where", nargs="?", default="all", choices=["all", "desktop", "startmenu"])

    install_cmd = sub.add_parser("install", help="autostart + shortcuts, so the app is always there")
    install_cmd.add_argument("--no-autostart", action="store_true")
    install_cmd.add_argument("--no-shortcuts", action="store_true")

    sub.add_parser("mcp", help="MCP server over stdio for AI agents (read-only)")
    sub.add_parser("cheat", help="print the query grammar")
    sub.add_parser("stats", help="show index size and settings")

    connect_cmd = sub.add_parser("connect", help="register the MCP server with Claude Desktop / Cursor")
    connect_cmd.add_argument(
        "target", nargs="?", default="all", choices=["all", "claude-desktop", "cursor", "vscode", "print"],
        help="which client config to edit (default: all that exist)",
    )
    connect_cmd.add_argument(
        "--transport", default="stdio", choices=["stdio", "http"],
        help="stdio launches the process per client; http uses the running app",
    )
    connect_cmd.add_argument("--port", type=int, default=8765, help="port for the http transport")
    connect_cmd.add_argument("--write", action="store_true", help="actually edit the files")
    connect_cmd.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    config = IndexConfig()
    config.vector_backend = args.vector_backend
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
    if args.command == "ocr":
        return _cmd_ocr(args, config)
    if args.command == "mcp":
        from .mcp import serve as serve_mcp

        return serve_mcp(db_path, config)
    if args.command == "cheat":
        return _cmd_cheat()
    if args.command == "connect":
        return _cmd_connect(args, config, db_path)
    if args.command == "serve":
        from .api import serve

        serve(db_path, config, port=args.port, open_browser=not args.no_browser)
        return 0
    if args.command == "app":
        return _cmd_app(args, config, db_path)
    if args.command == "autostart":
        return _cmd_autostart(args)
    if args.command == "shortcut":
        return _cmd_shortcut(args)
    if args.command == "install":
        return _cmd_install(args)
    return _cmd_stats(config, db_path)


def _cmd_shortcut(args) -> int:
    from . import shortcut

    if not shortcut.supported():
        print("shortcuts are Windows-only for now", file=sys.stderr)
        return 1
    try:
        if args.action == "remove":
            removed = shortcut.remove(args.where)
            for path in removed:
                print(f"removed {path}")
            if not removed:
                print("nothing to remove")
            return 0
        written = shortcut.create(args.where)
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"created {path}")
    return 0


def _cmd_install(args) -> int:
    from . import autostart, shortcut

    code = 0
    if not args.no_autostart:
        try:
            command = autostart.enable()
            print(f"autostart on: {command}")
        except Exception as exc:
            print(f"autostart failed: {exc}", file=sys.stderr)
            code = 1
    if not args.no_shortcuts:
        try:
            for path in shortcut.create("all"):
                print(f"shortcut: {path}")
        except Exception as exc:
            print(f"shortcuts failed: {exc}", file=sys.stderr)
            code = 1
    print()
    print("start it now:   run.py app")
    print("turn it off:    semfind autostart off && semfind shortcut remove")
    return code


def _cmd_autostart(args) -> int:
    from . import autostart

    if args.action == "on":
        try:
            command = autostart.enable()
        except RuntimeError as exc:
            print(exc, file=sys.stderr)
            return 1
        print(f"{autostart.VALUE_NAME} will start with Windows:")
        print(f"  {command}")
        print("turn it off with: semfind autostart off")
        return 0

    if args.action == "off":
        removed = autostart.disable()
        print("removed" if removed else "was not enabled")
        return 0

    state = autostart.status()
    if args.json:
        print(json.dumps(state, ensure_ascii=False))
        return 0
    if not state["supported"]:
        print("autostart is Windows-only for now")
        return 0
    print(f"{state['name']}: {'on' if state['enabled'] else 'off'}")
    print(f"command: {state.get('registered') or state['command']}")
    return 0


def _cmd_app(args, config: IndexConfig, db_path: Path) -> int:
    from .app import open_app

    config.ensure_home()
    if args.browser:
        from .api import serve

        serve(db_path, config, port=args.port, open_browser=True)
        return 0
    code = open_app(
        db_path=db_path,
        config=config,
        port=args.port,
        hotkey=None if args.no_hotkey else args.hotkey,
        tray=not args.no_tray,
        start_hidden=args.hidden,
        borderless=not args.windowed,
        debug=args.debug,
    )
    if code != 0:
        # No window available: the UI still has to open somehow.
        from .api import serve

        print("falling back to the browser", file=sys.stderr)
        serve(db_path, config, port=args.port, open_browser=True)
        return 0
    return code


def _cmd_index(args, config: IndexConfig, db_path: Path) -> int:
    config.ensure_home()
    if args.no_ocr:
        config.ocr_enabled = False
    if getattr(args, "allow_sensitive", False):
        config.allow_sensitive = True
    embedder = get_embedder(args.embedder, config.model_dir)
    visual = get_visual_embedder(config.model_dir)
    store = Store(db_path, embedder.name, embedder.dim, vector_backend=config.vector_backend)
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
    if stats.rehashed:
        print(f"rehashed       {stats.rehashed}  (phash backfill for burst grouping)")
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
    store = Store(db_path, embedder.name, embedder.dim, readonly=True, vector_backend=config.vector_backend)
    try:
        query = " ".join(args.query)
        started = time.perf_counter()
        page = search_page(
            store, embedder, query, limit=args.limit, offset=args.offset, sort=args.sort,
            parsed=understand(query), usage=store.usage_map(),
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
    finally:
        store.close()

    if args.json:
        payload = {
            "query": query,
            "ms": round(elapsed_ms, 1),
            "total": page.total,
            "approx": page.approx,
            "sort": page.sort,
            "chips": page.chips,
            "hits": [
                {
                    "path": hit.path,
                    "kind": hit.kind,
                    "score": hit.score,
                    "channels": list(hit.channels),
                    "snippet": hit.snippet,
                    "mtime": hit.mtime,
                    "size": hit.size,
                }
                for hit in page.hits
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if not page.hits:
        print(f"nothing found for «{query}» ({elapsed_ms:.0f} ms)")
        return 1

    chips = "  ".join(f"[{chip['label']}]" for chip in page.chips)
    header = f"{len(page.hits)} of {'≈' if page.approx else ''}{page.total} in {elapsed_ms:.0f} ms — «{query}»"
    print(header)
    if chips:
        print(chips)
    print()
    if args.files:
        for position, hit in enumerate(page.hits, start=1):
            print(f"{position:2}. {hit.score:.4f}  {hit.path}")
        return 0

    for position, hit in enumerate(page.hits, start=1):
        channels = "+".join(hit.channels)
        print(f"{position:2}. {hit.score:.4f}  [{channels}]  {hit.name}")
        print(f"    {hit.path}")
        if hit.snippet:
            print(f"    {hit.snippet}")
        print()
    return 0


def _cmd_connect(args, config: IndexConfig, db_path: Path) -> int:
    from . import connect

    transport = args.transport
    value = connect.entry(transport, config, args.port, db_path)

    if args.target == "print":
        print(connect.snippet(transport, config, args.port, db_path))
        return 0

    chosen = connect.targets() if args.target == "all" else [connect.find(args.target)]
    chosen = [target for target in chosen if target is not None]
    if not chosen:
        print(f"unknown target: {args.target}", file=sys.stderr)
        return 2

    results = []
    for target in chosen:
        exists = target.path.exists()
        # "all" only touches configs that already exist: creating a Cursor config
        # on a machine without Cursor is noise, not a feature.
        if args.target == "all" and not exists:
            results.append({"target": target.name, "path": str(target.path), "skipped": "not installed"})
            continue
        changed, message = connect.write(target, value, dry_run=not args.write)
        results.append({
            "target": target.name,
            "path": str(target.path),
            "written": bool(args.write) and changed,
            "message": message,
        })

    if args.json:
        print(json.dumps({"transport": transport, "results": results}, ensure_ascii=False, indent=2))
        return 0

    for item in results:
        if item.get("skipped"):
            print(f"  {item['target']:<16} пропущено ({item['skipped']})")
            continue
        verb = "записано" if item["written"] else "будет записано (--write, чтобы применить)"
        print(f"  {item['target']:<16} {verb}: {item['path']}")
        if not args.write:
            print()
            print(item["message"])
    if transport == "http":
        print(f"\nтокен лежит в {config.home / connect.TOKEN_FILE}; приложение должно быть запущено")
    else:
        print("\nклиент запустит `semfind mcp` сам, приложение может быть закрыто")
    return 0


def _cmd_cheat() -> int:
    from .query import cheat_sheet

    print("грамматика запроса\n")
    for syntax, meaning in cheat_sheet():
        print(f"  {syntax:<22} {meaning}")
    print("\nобычные фразы тоже работают:")
    for example in (
        "скриншоты за прошлую неделю",
        "самые большие файлы",
        "3 дня назад ошибка",
        "вчерашние документы",
        "pdf про индексы",
    ):
        print(f"  {example}")
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
    store = Store(db_path, embedder.name, embedder.dim, readonly=True, vector_backend=config.vector_backend)
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


def _cmd_ocr(args, config: IndexConfig) -> int:
    from .ocr import coverage, ocr_image
    from .ocr import engine as ocr_engine
    from .ocr import languages as ocr_languages

    if args.action == "languages":
        cover = coverage()
        if args.json:
            print(json.dumps(cover, ensure_ascii=False, indent=2))
            return 0
        print(f"windows packs  {', '.join(cover['windows']) or 'none'}")
        print(f"models         {', '.join(cover['models']) or 'none'}")
        print(f"readable now   {cover['total']} languages")
        missing = [model for model in ocr_languages.MODELS if model not in cover["models"]]
        if missing:
            print(f"one download   {', '.join(missing)}  (semfind ocr download all)")
        for language in cover["languages"]:
            print(f"  {language['tag']:<8} {language['name']:<18} {language['hello']}")
        return 0

    if args.action == "download":
        target = args.target or "all"
        models = list(ocr_languages.MODELS) if target in ("all", "*") else [target]
        failed = 0
        for model in models:
            if model in ("multi",):
                continue  # ships with the package
            engine = ocr_engine.RapidOcrMulti(model)
            print(f"{model:<12}", end=" ", flush=True)
            try:
                engine.recognize(_probe_image())
            except Exception as exc:
                failed += 1
                print(f"failed: {str(exc)[:120]}")
                continue
            print("ok")
        print(f"models on disk: {', '.join(sorted(ocr_engine.downloaded_models())) or 'none'}")
        return 1 if failed else 0

    if not args.target:
        print("usage: semfind ocr read <image> [--lang korean]", file=sys.stderr)
        return 2
    preferred = f"rapidocr:{args.lang}" if getattr(args, "lang", None) else None
    result = ocr_image(args.target, config.ocr_languages, preferred)
    if args.json:
        print(json.dumps({"engine": result.engine, "text": result.text}, ensure_ascii=False, indent=2))
    else:
        print(f"[{result.engine}]")
        print(result.text)
    return 0


def _probe_image():
    """An image with text on it, so the recogniser actually loads its model.

    A blank canvas only exercises the detector: rapidocr builds the recognition
    model lazily, on the first text box it finds, so `download` against a blank
    image reports success and downloads nothing.
    """
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (480, 120), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.load_default(size=56)
    except TypeError:  # Pillow < 10 has no size argument
        font = ImageFont.load_default()
    draw.text((20, 30), "Test 123 hello", font=font, fill="black")
    return image


def _cmd_privacy(args, config: IndexConfig, db_path: Path) -> int:
    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    embedder = get_embedder("auto", config.model_dir)
    if args.action == "purge":
        store = Store(db_path, embedder.name, embedder.dim, vector_backend=config.vector_backend)
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

    store = Store(db_path, embedder.name, embedder.dim, readonly=True, vector_backend=config.vector_backend)
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
    store = Store(db_path, embedder.name, embedder.dim, readonly=True, vector_backend=config.vector_backend)
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
