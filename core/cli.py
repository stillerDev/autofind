"""Command line entry point: autofind index / search / serve / stats."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# The command modules are imported inside `main`/handlers: importing them here
# would make every cold start pay for search/store machinery the command may
# not touch (and would drag numpy into one-shot searches).


def get_embedder(*args, **kwargs):
    # Imported lazily: the embedder module pulls numpy and Pillow, and a cold
    # CLI search should not pay for the visual stack it never touches.
    from .embed import get_embedder as _impl

    return _impl(*args, **kwargs)


def get_visual_embedder(*args, **kwargs):
    from .embed import get_visual_embedder as _impl

    return _impl(*args, **kwargs)

# doctor / evaluate / indexer / ocr / privacy / watch are imported inside the
# subcommands that need them: `autofind search` would otherwise pay ~200 ms of
# startup (doctor alone pulls the OCR stack) for machinery it never touches.

BAR_WIDTH = 28


def main(argv: list[str] | None = None) -> int:
    _force_utf8()
    parser = argparse.ArgumentParser(
        prog="autofind", description="Local search over files and screenshots. Nothing leaves the machine."
    )
    parser.add_argument("--db", type=Path, help="path to index.db (default: <AUTOFIND_HOME>/index.db)")
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
    index_cmd.add_argument("--embedder", default="auto", choices=["auto", "e5", "static", "hashing"])
    index_cmd.add_argument("--workers", type=int, default=None,
                           help="extraction worker processes (0 = auto, 1 = single process)")
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
    search_cmd.add_argument("--embedder", default="auto", choices=["auto", "e5", "static", "hashing"])

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

    seal_cmd = sub.add_parser(
        "seal",
        help="paranoid mode: encrypt all content in the index (on | off | status)",
    )
    seal_cmd.add_argument("action", nargs="?", default="status", choices=["on", "off", "status"])

    export_cmd = sub.add_parser("export", help="export search results as CSV or JSON")
    export_cmd.add_argument("query", help="search query")
    export_cmd.add_argument("--format", default="csv", choices=["csv", "json", "md"])
    export_cmd.add_argument("--limit", type=int, default=100)
    export_cmd.add_argument("--out", dest="out", type=Path, default=None,
                            help="write to a file instead of stdout")

    uninstall_cmd = sub.add_parser("uninstall", help="remove index, vault key, autostart")
    uninstall_cmd.add_argument("--confirm", action="store_true")

    sub.add_parser("clip", help="download the CLIP model for text-to-image visual search")

    backup_cmd = sub.add_parser("backup", help="pack the index and sidecars into a timestamped ZIP")
    backup_cmd.add_argument("--to", dest="to_dir", default=None, help="target directory (default: the desktop)")

    share_cmd = sub.add_parser("share", help="copy the results of a query as markdown to the clipboard")
    share_cmd.add_argument("query")
    share_cmd.add_argument("--limit", type=int, default=10)

    open_cmd = sub.add_parser("open", help="open the top hit of a query right now")
    open_cmd.add_argument("query")

    openq_top = sub.add_parser("pin-top", help="pin the top hit of a query")
    openq_top.add_argument("query")

    tag_cmd = sub.add_parser("tag", help="add or remove a tag on a file")
    tag_cmd.add_argument("target", help="file id or path")
    tag_cmd.add_argument("tag")
    tag_cmd.add_argument("--off", action="store_true", help="remove the tag")

    sub.add_parser("watch-status", help="show watched roots and watcher state")

    stats_cmd = sub.add_parser("stats", help="show index size and settings")
    stats_cmd.add_argument("--verbose", action="store_true", help="per-kind counts and top extensions")


    changelog_cmd = sub.add_parser("changelog", help="print the what's-new notes")
    changelog_cmd.add_argument("--lines", type=int, default=40)

    register_url = sub.add_parser("register-url", help="handle autofind:// links (search from anywhere)")
    register_url.add_argument("--remove", action="store_true")

    start_min = sub.add_parser("config", help="show or set a preference (start_minimized, content_skip_over_mb)")
    start_min.add_argument("key", nargs="?")
    start_min.add_argument("value", nargs="?")

    verify_cmd = sub.add_parser("verify", help="compare the index against disk: missing, changed, extra")
    verify_cmd.add_argument("--prune", action="store_true", help="drop missing paths from the index")

    update_cmd = sub.add_parser("update", help="check GitHub for a newer release and install it")
    update_cmd.add_argument("--check", action="store_true",
                            help="only check for an update, do not install")

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

    big_cmd = sub.add_parser("big", help="largest files in the index")
    big_cmd.add_argument("min_mb", nargs="?", type=float, default=10.0)
    big_cmd.add_argument("--limit", type=int, default=50)
    big_cmd.add_argument("--json", action="store_true")

    stale_cmd = sub.add_parser("stale", help="files not modified in N days")
    stale_cmd.add_argument("days", nargs="?", type=float, default=365.0)
    stale_cmd.add_argument("--limit", type=int, default=50)
    stale_cmd.add_argument("--json", action="store_true")

    dupes_cmd = sub.add_parser("dupes", help="files with identical content (sha256)")
    dupes_cmd.add_argument("--limit", type=int, default=50)
    dupes_cmd.add_argument("--json", action="store_true")

    dead_cmd = sub.add_parser("dead", help="indexed paths that no longer exist")
    dead_cmd.add_argument("--prune", action="store_true",
                          help="remove dead paths from the index, not just list them")
    dead_cmd.add_argument("--json", action="store_true")

    top_cmd = sub.add_parser("top", help="files you open the most")
    top_cmd.add_argument("--limit", type=int, default=25)
    top_cmd.add_argument("--json", action="store_true")

    pin_cmd = sub.add_parser("pin", help="pin a file so it sits on the empty screen")
    pin_cmd.add_argument("target", help="file id, or a path inside the index")
    pin_cmd.add_argument("--off", action="store_true", help="unpin instead of pin")
    pin_cmd.add_argument("--json", action="store_true")

    pins_cmd = sub.add_parser("pins", help="list pinned files")
    pins_cmd.add_argument("--json", action="store_true")

    saved_cmd = sub.add_parser("saved", help="list, add or drop saved searches")
    saved_cmd.add_argument("query", nargs="*", help="query to save; omit to list")
    saved_cmd.add_argument("--label", default="", help="name to show instead of the query")
    saved_cmd.add_argument("--drop", type=int, default=0, metavar="ID", help="forget this saved search")
    saved_cmd.add_argument("--json", action="store_true")

    alias_cmd = sub.add_parser("alias", help="list or edit the personal dictionary")
    alias_cmd.add_argument("alias", nargs="?", default=None)
    alias_cmd.add_argument("expand", nargs="?", default=None)
    alias_cmd.add_argument("--drop", default=None, metavar="ALIAS", help="forget this alias")
    alias_cmd.add_argument("--json", action="store_true")

    note_cmd = sub.add_parser("note", help="a human note on a file (\"why did I keep this?\")")
    note_cmd.add_argument("target", help="file id, or a path inside the index")
    note_cmd.add_argument("text", nargs="*", help="the note; omit text or --off to clear")
    note_cmd.add_argument("--off", action="store_true", help="remove the note")

    sym_cmd = sub.add_parser("sym", help="find definitions in code (functions, classes, types)")
    sym_cmd.add_argument("name", help="symbol name, or a prefix of it")
    sym_cmd.add_argument("-n", "--limit", type=int, default=20)
    sym_cmd.add_argument("--json", action="store_true")

    import_cmd = sub.add_parser(
        "import", help="bring pins, saved searches and the dictionary over from another app"
    )
    import_cmd.add_argument("source", nargs="?", default="kensaku", choices=["kensaku"])
    import_cmd.add_argument("path", nargs="?", default=None, help="a specific config file or folder")
    import_cmd.add_argument("--write", action="store_true", help="actually copy them in")
    import_cmd.add_argument("--json", action="store_true")

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
    from .config import IndexConfig

    config = IndexConfig()
    config.vector_backend = args.vector_backend
    db_path = args.db or config.db_path
    try:
        from . import vault as _vault_mod

        _vault_mod.load(config.home)
    except Exception:
        pass  # not sealed, or the key is gone - seal/search surface it

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
    if args.command == "seal":
        return _cmd_seal(args, config, db_path)
    if args.command == "export":
        return _cmd_export(args, config, db_path)
    if args.command == "uninstall":
        return _cmd_uninstall(args, config)
    if args.command in ("big", "stale", "dupes", "dead", "top"):
        return _cmd_finder(args, config, db_path)
    if args.command == "backup":
        return _cmd_backup(args, config, db_path)
    if args.command == "share":
        return _cmd_share(args, config, db_path)
    if args.command == "open":
        return _cmd_open_top(args, config, db_path)
    if args.command == "pin-top":
        return _cmd_pin_top(args, config, db_path)
    if args.command == "tag":
        return _cmd_tag(args, config, db_path)
    if args.command == "watch-status":
        return _cmd_watch_status(args, config, db_path)
    if args.command == "register-url":
        return _cmd_register_url(args)
    if args.command == "changelog":
        return _cmd_changelog(args)
    if args.command == "config":
        return _cmd_config(args, config)
    if args.command == "compact":
        from .store import Store
        if not db_path.exists():
            print(f"no index at {db_path}", file=sys.stderr)
            return 2
        store = _open_store(config, db_path, readonly=False)
        try:
            t0 = time.perf_counter()
            store.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            store.conn.execute("VACUUM")
            store.conn.commit()
            print(f"compacted in {time.perf_counter() - t0:.1f}s")
        finally:
            store.close()
        return 0
    if args.command == "verify":
        return _cmd_verify(args, config, db_path)
    if args.command == "clip":
        # tools/ ships as loose scripts, not a package: load by path so an
        # installed copy finds the fetcher next to its own module tree.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "autofind_fetch_clip", Path(__file__).resolve().parents[1] / "tools" / "fetch_clip.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.main()

    if args.command == "update":
        from .updater import check_update, download_update, apply_update, UpdateError, current_version, parse_version
        if args.check:
            info = check_update()
            if info:
                print(f"update available: v{current_version()} -> v{info['version']}")
                print(f"download: {info['url']}")
            else:
                print(f"up to date (v{current_version()})")
            return 0
        info = check_update()
        if not info:
            print(f"up to date (v{current_version()})")
            return 0
        print(f"downloading v{info['version']} ({info.get('name', 'update')})...")
        try:
            staged = download_update(info["url"], info.get("size", 0))
            apply_update(staged)
            print("update installed — restarting")
        except (UpdateError, OSError) as exc:
            print(f"update failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "autostart":
        return _cmd_autostart(args)
    if args.command == "shortcut":
        return _cmd_shortcut(args)
    if args.command == "install":
        return _cmd_install(args)
    if args.command == "pin":
        return _cmd_pin(args, config, db_path)
    if args.command == "note":
        return _cmd_note(args, config, db_path)
    if args.command == "pins":
        return _cmd_pins(args, config, db_path)
    if args.command == "saved":
        return _cmd_saved(args, config, db_path)
    if args.command == "alias":
        return _cmd_alias(args, config, db_path)
    if args.command == "sym":
        return _cmd_sym(args, config, db_path)
    if args.command == "import":
        return _cmd_import(args, config, db_path)
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
    print("turn it off:    autofind autostart off && autofind shortcut remove")
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
        print("turn it off with: autofind autostart off")
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
    from .indexer import run_index
    from .ocr import engine_report
    from .store import Store

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
            workers=args.workers,
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
    from .personal import expand_aliases
    from .query import understand
    from .search import search_page
    from .store import Store

    if not db_path.exists():
        print(f"no index at {db_path}; run: autofind index <folder>", file=sys.stderr)
        return 2
    embedder = get_embedder(args.embedder, config.model_dir)
    store = Store(db_path, embedder.name, embedder.dim, readonly=True,
                  vector_backend=config.vector_backend, ram_bm25=False)
    try:
        query = " ".join(args.query)
        started = time.perf_counter()
        # Expand the dictionary before parsing, so `cv` behaves the same here as
        # it does in the window.
        query, applied = expand_aliases(store, query)
        page = search_page(
            store, embedder, query, limit=args.limit, offset=args.offset, sort=args.sort,
            parsed=understand(query), usage=store.usage_map(), applied=applied,
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
        print("\nклиент запустит `autofind mcp` сам, приложение может быть закрыто")
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
    from .watch import Watcher, watch_forever

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
    from .evaluate import compare_channels, evaluate, format_report, load_labels
    from .store import Store

    if not db_path.exists():
        print(f"no index at {db_path}; run: autofind index <folder>", file=sys.stderr)
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
    from .doctor import collect as collect_doctor
    from .doctor import format_report as format_doctor

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
            print(f"one download   {', '.join(missing)}  (autofind ocr download all)")
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
        print("usage: autofind ocr read <image> [--lang korean]", file=sys.stderr)
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
    from .privacy import indexed_sensitive, purge_indexed
    from .store import Store

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
    print("\nremove them with: autofind privacy purge")
    return 0


def _cmd_stats(config: IndexConfig, db_path: Path) -> int:
    from .ocr import engine_report
    from .store import Store

    if not db_path.exists():
        print(f"no index at {db_path}")
        return 2
    embedder = get_embedder("auto", config.model_dir)
    visual = get_visual_embedder(config.model_dir)
    store = Store(db_path, embedder.name, embedder.dim, readonly=True, vector_backend=config.vector_backend)
    try:
        stats = store.stats()
        symbols = store.symbol_count()
        pins = len(store.pinned_ids())
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
    print(f"definitions    {symbols}")
    print(f"pinned         {pins}")
    print(f"size           {stats['db_bytes'] / 1024 / 1024:.1f} MB")
    return 0


def _open_store(config: IndexConfig, db_path: Path, readonly: bool = True) -> Store:
    from .store import Store

    embedder = get_embedder("auto", config.model_dir)
    return Store(
        db_path, embedder.name, embedder.dim,
        readonly=readonly, vector_backend=config.vector_backend,
    )


def _cmd_pin(args, config: IndexConfig, db_path: Path) -> int:
    from .personal import pin

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path, readonly=False)
    try:
        target = args.target.strip()
        file_id = 0
        if target.isdigit():
            row = store.file_by_id(int(target))
            file_id = int(row["id"]) if row else 0
        if not file_id:
            # A path is matched the same forgiving way the import matches one:
            # separators and case are not something a user should have to get right.
            resolved = store.resolve_paths([target])
            file_id = int(resolved.get(target, 0))
        if not file_id:
            print(f"not in the index: {target}", file=sys.stderr)
            return 1
        state = pin(store, file_id, not args.off)
        store.commit()
        row = store.file_by_id(file_id)
    finally:
        store.close()
    if args.json:
        print(json.dumps({"id": file_id, "pinned": state, "path": row["path"]}, ensure_ascii=False))
    else:
        print(f"{'pinned' if state else 'unpinned'} {row['path']}")
    return 0


def _cmd_backup(args, config: IndexConfig, db_path: Path) -> int:
    """A timestamped ZIP of the whole index: safe to copy, safe to restore."""
    from .transfer import export_index

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    target_dir = Path(args.to_dir) if args.to_dir else (
        Path.home() / "Desktop")
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = target_dir / f"autofind-backup-{stamp}.zip"
    manifest = export_index(db_path, archive)
    print(f"backup: {archive}")
    print(f"  files in archive: {len(manifest['entries'])}")
    return 0


def _results_markdown(store, embedder, query: str, limit: int) -> str:
    from .query import understand
    from .search import search_page

    page = search_page(store, embedder, query, limit=limit,
                       parsed=understand(query), usage=store.usage_map())
    lines = [f"# AutoFind: {query}", ""]
    for hit in page.hits:
        lines.append(f"- **{hit.name}** — `{hit.path}` ({hit.kind}, {hit.size} B)")
        if hit.snippet:
            lines.append(f"  > {hit.snippet[:160]}")
    return "\n".join(lines)


def _cmd_share(args, config: IndexConfig, db_path: Path) -> int:
    """Results as markdown straight onto the clipboard: chat-ready."""
    from .embed import get_embedder
    from .store import Store

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    emb = get_embedder("auto", config.model_dir)
    store = Store(db_path, emb.name, emb.dim, readonly=True)
    try:
        text = _results_markdown(store, emb, args.query, args.limit)
    finally:
        store.close()
    try:
        import subprocess
        subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True)
        print(f"copied {text.count(chr(10))} lines to the clipboard")
        return 0
    except (OSError, subprocess.CalledProcessError):
        print(text)
        return 0


def _top_hit(store, embedder, query: str):
    from .query import understand
    from .search import search_page

    page = search_page(store, embedder, query, limit=1,
                       parsed=understand(query), usage=store.usage_map())
    return page.hits[0] if page.hits else None


def _cmd_open_top(args, config: IndexConfig, db_path: Path) -> int:
    """`autofind open invoice` - the best hit opens, no UI round trip."""
    from .embed import get_embedder
    from .store import Store

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    emb = get_embedder("auto", config.model_dir)
    store = Store(db_path, emb.name, emb.dim, readonly=True)
    try:
        hit = _top_hit(store, emb, args.query)
    finally:
        store.close()
    if hit is None:
        print("no results", file=sys.stderr)
        return 1
    print(hit.path)
    os.startfile(hit.path)  # noqa: S606 - a local indexed file, user-asked
    return 0


def _cmd_pin_top(args, config: IndexConfig, db_path: Path) -> int:
    """Pin the top hit of a query without leaving the terminal."""
    from .embed import get_embedder
    from .store import Store

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    emb = get_embedder("auto", config.model_dir)
    store = Store(db_path, emb.name, emb.dim, readonly=True)
    try:
        hit = _top_hit(store, emb, args.query)
    finally:
        store.close()
    if hit is None:
        print("no results", file=sys.stderr)
        return 1
    store = _open_store(config, db_path, readonly=False)
    try:
        store.set_pin(int(hit.file_id), True)
        store.commit()
        print(f"pinned {hit.path}")
    finally:
        store.close()
    return 0


def _cmd_tag(args, config: IndexConfig, db_path: Path) -> int:
    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path, readonly=False)
    try:
        target = args.target.strip()
        file_id = 0
        if target.isdigit():
            row = store.file_by_id(int(target))
            file_id = int(row["id"]) if row else 0
        if not file_id:
            resolved = store.resolve_paths([target])
            file_id = int(resolved.get(target, 0))
        if not file_id:
            print(f"not in the index: {target}", file=sys.stderr)
            return 1
        store.set_tag(file_id, args.tag, on=not args.off)
        store.commit()
        print(("tag removed" if args.off else "tagged") + f": {args.tag}")
    finally:
        store.close()
    return 0


def _cmd_watch_status(args, config: IndexConfig, db_path: Path) -> int:
    """Which roots are watched and whether the watcher is alive."""
    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path)
    try:
        roots = [row[0] for row in store.conn.execute(
            "SELECT DISTINCT substr(path, 1, 3) FROM files"
        ).fetchall()]
    finally:
        store.close()
    # The watcher keeps a lock file with its pid while it runs.
    alive = False
    lock = Path(str(db_path) + ".watch")
    if lock.exists():
        try:
            import psutil
            pid = int(lock.read_text(encoding="utf-8").strip() or 0)
            alive = bool(pid) and psutil.pid_exists(pid)
        except (ValueError, OSError, ImportError):
            alive = lock.exists()
    print(f"watcher alive: {'yes' if alive else 'no'}")
    for root in roots:
        print(f"  root: {root}")
    return 0


def _cmd_register_url(args) -> int:
    """autofind://search?q=... from the Run dialog or any app."""
    import winreg

    key_path = r"Software\Classes\autofind"
    if args.remove:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key_path)
            print("autofind:// handler removed")
        except FileNotFoundError:
            print("no handler registered")
        return 0
    command = f'"{sys.executable}" "{Path(__file__).resolve().parent.parent / "run.py"}" --url "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "URL Protocol", 0, winreg.REG_SZ, "")
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, "URL:AutoFind Search")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path + r"\shell\open\command") as key:
        winreg.SetValueEx(key, None, 0, winreg.REG_SZ, command)
    print("autofind:// registered")
    return 0


def _cmd_changelog(args) -> int:
    for name in ("WHATSNEW.TXT", "CHANGELOG.md"):
        path = Path(__file__).resolve().parent.parent / name
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            print("\n".join(lines[:args.lines]))
            return 0
    print("no changelog file in this build")
    return 0


def _cmd_config(args, config: IndexConfig) -> int:
    """Show or set a preference: start_minimized, content_skip_over_mb."""
    prefs_path = config.home / "prefs.json"
    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        prefs = {}
    if not args.key:
        print(json.dumps(prefs, indent=2, ensure_ascii=False))
        return 0
    config.ensure_home()
    if args.value is None:
        print(json.dumps(prefs.get(args.key), indent=2, ensure_ascii=False))
        return 0
    value: object = args.value
    if args.value.lower() in ("on", "true", "1"):
        value = True
    elif args.value.lower() in ("off", "false", "0"):
        value = False
    else:
        try:
            value = int(args.value)
        except ValueError:
            pass
    prefs[args.key] = value
    config.ensure_home()
    prefs_path.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{args.key} = {value!r}")
    return 0


def _cmd_finder(args, config: IndexConfig, db_path: Path) -> int:
    """big / stale / dupes / dead / top — corpus questions, one SQL each."""
    from . import finders

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path)
    try:
        if args.command == "big":
            rows = finders.biggest(store.conn, min_mb=args.min_mb, limit=args.limit)
        elif args.command == "stale":
            rows = finders.stale(store.conn, days=args.days, limit=args.limit)
        elif args.command == "dupes":
            rows = finders.duplicates(store.conn, limit=args.limit)
        elif args.command == "dead":
            rows = finders.dead_paths(store.conn)
            if args.prune and rows:
                removed = finders.prune_dead(store, rows)
                print(f"pruned {removed} dead paths")
        else:  # top
            rows = finders.most_opened(store.conn, limit=args.limit)
    finally:
        store.close()

    if args.json:
        print(json.dumps(
            [{"name": r.name, "path": r.path, "kind": r.kind, "size": r.size,
              "mtime": r.mtime, "score": r.score} for r in rows],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not rows:
        print("nothing found")
        return 0
    for r in rows:
        size_mb = r.size / 1048576
        print(f"  {size_mb:9.1f} MB  {r.path}" if args.command == "big"
              else f"  {r.score:>6}  {r.path}")
    return 0


def _cmd_verify(args, config: IndexConfig, db_path: Path) -> int:
    """Index vs disk: missing paths, changed sizes, and a --prune that drops them."""
    from . import finders

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path, readonly=not args.prune)
    try:
        rows = store.conn.execute("SELECT id, path, size, mtime FROM files").fetchall()
        missing = []
        changed = 0
        for row in rows:
            try:
                st = os.stat(row["path"])
            except OSError:
                missing.append((int(row["id"]), row["path"]))
                continue
            if st.st_size != int(row["size"] or 0):
                changed += 1
        for _, path in missing:
            print(f"  missing: {path}")
        print(f"checked {len(rows)}: {len(missing)} missing, {changed} size-changed")
        if args.prune and missing:
            found = [finders.Found(path=path, name="", kind="", size=0, mtime=0)
                     for _, path in missing]
            removed = finders.prune_dead(store, found)
            print(f"pruned {removed} missing paths")
    finally:
        store.close()
    return 0


def _cmd_note(args, config: IndexConfig, db_path: Path) -> int:
    """Set, replace or clear the human note on one file."""
    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path, readonly=False)
    try:
        target = args.target.strip()
        file_id = 0
        if target.isdigit():
            row = store.file_by_id(int(target))
            file_id = int(row["id"]) if row else 0
        if not file_id:
            resolved = store.resolve_paths([target])
            file_id = int(resolved.get(target, 0))
        if not file_id:
            print(f"not in the index: {target}", file=sys.stderr)
            return 1
        text = " ".join(args.text or []).strip()
        if args.off or not text:
            store.set_note(file_id, None)
            print("note cleared")
        else:
            store.set_note(file_id, text)
            print(f"note saved on {target}")
    finally:
        store.close()
    return 0


def _cmd_pins(args, config: IndexConfig, db_path: Path) -> int:
    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path)
    try:
        rows = store.pinned_rows(limit=200)
    finally:
        store.close()
    if args.json:
        print(json.dumps(
            [{"id": int(row["id"]), "path": row["path"], "kind": row["kind"]} for row in rows],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not rows:
        print("nothing pinned yet: autofind pin <path>")
        return 0
    for row in rows:
        print(f"  {int(row['id']):<7} {row['path']}")
    return 0


def _cmd_saved(args, config: IndexConfig, db_path: Path) -> int:
    from .personal import drop, save

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path, readonly=False)
    try:
        if args.drop:
            gone = drop(store, args.drop)
            store.close()
            if args.json:
                print(json.dumps({"dropped": gone}))
            else:
                print("forgotten" if gone else "no such saved search")
            return 0 if gone else 1
        if args.query:
            item = save(store, " ".join(args.query), args.label)
            store.close()
            if args.json:
                print(json.dumps(item, ensure_ascii=False))
            else:
                print(f"saved #{item['id']}: {item['query']}")
            return 0
        rows = store.saved_searches()
        store.close()
    except ValueError as exc:
        store.close()
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(
            [{"id": int(row["id"]), "query": row["query"], "label": row["label"],
              "used": int(row["used"] or 0)} for row in rows],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not rows:
        print("no saved searches yet: autofind saved \"type:pdf про индексы\"")
        return 0
    for row in rows:
        used = f"  ×{int(row['used'] or 0)}" if row["used"] else ""
        print(f"  {int(row['id']):<4} {row['query']}{used}")
    return 0


def _cmd_alias(args, config: IndexConfig, db_path: Path) -> int:
    from .personal import forget, teach

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path, readonly=False)
    try:
        if args.drop:
            gone = forget(store, args.drop)
            store.close()
            if args.json:
                print(json.dumps({"dropped": gone}))
            else:
                print(f"forgot {args.drop}" if gone else "no such alias")
            return 0 if gone else 1
        if args.alias and args.expand:
            item = teach(store, args.alias, args.expand)
            store.close()
            if args.json:
                print(json.dumps(item, ensure_ascii=False))
            else:
                print(f"{item['alias']} → {item['expand']}")
            return 0
        table = store.aliases()
        store.close()
    except ValueError as exc:
        store.close()
        print(str(exc), file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(table, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if not table:
        print('dictionary is empty: autofind alias cv "резюме"')
        return 0
    for alias, expand in sorted(table.items()):
        print(f"  {alias:<16} → {expand}")
    return 0


def _cmd_sym(args, config: IndexConfig, db_path: Path) -> int:
    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    store = _open_store(config, db_path)
    try:
        rows = store.find_symbols(args.name, args.limit)
    finally:
        store.close()
    if args.json:
        print(json.dumps(
            [{"name": row["name"], "kind": row["kind"], "line": int(row["line"]),
              "path": row["path"], "text": row["text"]} for row in rows],
            ensure_ascii=False, indent=2,
        ))
        return 0
    if not rows:
        print(f"no definition matches {args.name!r}")
        return 1
    for row in rows:
        print(f"  {row['kind']:<9} {row['name']:<28} {row['path']}:{int(row['line'])}")
    return 0


def _cmd_import(args, config: IndexConfig, db_path: Path) -> int:
    from .migrate import apply, find_sources

    if not db_path.exists():
        print(f"no index at {db_path}; run: autofind index <folder>", file=sys.stderr)
        return 2
    extra = [args.path] if args.path else []
    sources = find_sources(extra)
    if not sources:
        print("no kensaku config found.")
        print("point at it directly:  autofind import kensaku \"C:\\Users\\you\\AppData\\Roaming\\kensaku\"")
        return 1
    store = _open_store(config, db_path, readonly=False)
    try:
        report = apply(store, sources, write=args.write)
    finally:
        store.close()

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    verb = "copied" if args.write else "would copy"
    for item in report["sources"]:
        print(f"  {item['path']}")
        print(f"    {item['pins']} pins · {item['saved']} saved · {item['aliases']} aliases")
    print()
    print(f"{verb} {report['pins']} pins, {report['saved']} saved searches, {report['aliases']} aliases")
    if report["missing"]:
        print(f"{len(report['missing'])} pinned file(s) are not in the index yet:")
        for path in report["missing"][:5]:
            print(f"  · {path}")
        if len(report["missing"]) > 5:
            print(f"  … and {len(report['missing']) - 5} more")
        print("index those folders and run the import again")
    if not args.write:
        print("\nnothing written; add --write to keep it")
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


def _cmd_seal(args, config: IndexConfig, db_path: Path) -> int:
    """Paranoid mode: encrypt (or decrypt) every content blob in the index."""
    from . import vault as vault_mod
    from .store import Store

    action = args.action
    if action == "status":
        sealed = False
        try:
            store = Store(db_path, "hashing-384", 384, readonly=True)
            sealed = store.sealed
            store.close()
        except Exception as exc:
            print(f"status: cannot open the index ({exc})")
            return 1
        key = vault_mod.key_path(config.home)
        print(f"sealed        {sealed}")
        print(f"vault key     {key} ({'present' if key.exists() else 'missing'})")
        if sealed and not key.exists():
            print("WARNING: the index is sealed but the key is gone - "
                  "content is unreadable until vault.key is restored or the index is rebuilt")
        return 0

    if not db_path.exists():
        print(f"no index at {db_path}; run: autofind index <folder>", file=sys.stderr)
        return 2

    if action == "on":
        try:
            vault = vault_mod.enable(config.home)
        except vault_mod.VaultError as exc:
            print(f"cannot enable paranoid mode: {exc}", file=sys.stderr)
            return 1
        store = Store(db_path, "hashing-384", 384, readonly=False, vector_backend="pack")
        try:
            t0 = time.perf_counter()
            store.seal_index()
            print(f"sealed in {time.perf_counter() - t0:.1f}s")
            print(f"vault key     {vault_mod.key_path(config.home)}")
            print("WARNING: vault.key is bound to this Windows account - "
                  "if it is deleted, sealed content is unreadable and the index must be rebuilt")
        finally:
            store.close()
        return 0

    if action == "off":
        try:
            vault_mod.load(config.home)
        except vault_mod.VaultError as exc:
            print(f"cannot decrypt: {exc}", file=sys.stderr)
            return 1
        store = Store(db_path, "hashing-384", 384, readonly=False, vector_backend="pack")
        try:
            t0 = time.perf_counter()
            store.unseal_index()
            print(f"unsealed in {time.perf_counter() - t0:.1f}s (FTS rebuilt)")
        finally:
            store.close()
        return 0

    print(f"unknown action {action!r}", file=sys.stderr)
    return 2


def _cmd_export(args, config: IndexConfig, db_path: Path) -> int:
    """Export search results as CSV or JSON, mirroring the web export."""
    import csv as _csv
    import io as _io

    from .embed import get_embedder
    from .query import understand
    from .search import search_page
    from .store import Store

    if not db_path.exists():
        print(f"no index at {db_path}", file=sys.stderr)
        return 2
    emb = get_embedder("auto", config.model_dir)
    store = Store(db_path, emb.name, emb.dim, readonly=True)
    try:
        page = search_page(store, emb, args.query, limit=args.limit,
                          parsed=understand(args.query), usage=store.usage_map())
        tags = store.tags_for(hit.file_id for hit in page.hits)
    finally:
        store.close()

    rows = page.hits
    fmt = args.format
    if fmt == "md":
        lines = [f"# AutoFind: {args.query}", ""]
        for hit in rows:
            lines.append(f"- **{hit.name}** — `{hit.path}` ({hit.kind}, {hit.size} B)")
            if hit.snippet:
                lines.append(f"  > {hit.snippet[:160]}")
        body = "\n".join(lines).encode("utf-8")
    elif fmt == "csv":

        buffer = _io.StringIO()
        writer = _csv.writer(buffer)
        writer.writerow(["name", "path", "kind", "size", "modified", "score", "opens", "tags"])
        for hit in rows:
            writer.writerow([
                hit.name,
                hit.path,
                hit.kind,
                hit.size,
                time.strftime("%Y-%m-%d %H:%M", time.localtime(hit.mtime)),
                round(float(hit.score), 6),
                hit.opens,
                "; ".join(tags.get(hit.file_id, [])),
            ])
        # The BOM is what makes Excel open a UTF-8 CSV as UTF-8 instead of
        # mangling every Cyrillic file name — same as the web export.
        body = buffer.getvalue().encode("utf-8-sig")
    else:
        payload = {
            "query": args.query,
            "total": page.total,
            "exported": len(rows),
            "hits": [
                {
                    "file_id": hit.file_id,
                    "name": hit.name,
                    "path": hit.path,
                    "kind": hit.kind,
                    "size": hit.size,
                    "mtime": hit.mtime,
                    "score": round(float(hit.score), 6),
                    "opens": hit.opens,
                    "tags": tags.get(hit.file_id, []),
                }
                for hit in rows
            ],
        }
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    out = args.out
    if out:
        Path(out).write_bytes(body)
        print(f"exported {len(rows)} results to {out}")
    else:
        sys.stdout.buffer.write(body)
        sys.stdout.buffer.write(b"\n")
    return 0


def _cmd_uninstall(args, config: IndexConfig) -> int:
    """Clean uninstaller: removes the whole home, autostart, shortcuts."""
    from .autostart import disable as _disable_autostart
    from .shortcut import remove as _remove_shortcuts

    home = config.home
    if not home.exists():
        print("nothing to remove")
        return 0

    if getattr(args, "yes", False):
        import shutil
        try:
            shutil.rmtree(home)
        except OSError as exc:
            print(f"failed to remove {home}: {exc}", file=sys.stderr)
            return 1
        _disable_autostart()
        _remove_shortcuts()
        print("uninstalled")
        return 0
    else:
        key = home / "vault.key"
        key_status = "present" if key.exists() else "missing"
        print(f"dry run - vault key={key_status}")
        print(f"index={home / 'index.db'}")
        return 0
