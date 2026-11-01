"""Local HTTP API and static UI server.

Stdlib only: ThreadingHTTPServer, no FastAPI. The plan names FastAPI, but the
deliverable that matters is a UI that starts with one command and no install
step; the routes are the same, so swapping the server later is a contained
change.

Bound to 127.0.0.1 and the Host header is checked, which closes the trivial
DNS-rebinding path to the index. File-open endpoints take an index id, never a
raw path, so a page cannot ask us to open something we never scanned.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import numpy as np

from .config import IndexConfig
from .connect import token as mcp_token
from .embed import get_embedder, get_visual_embedder
from .indexer import IndexStats, run_index
from .mcp import McpServer
from .ocr import engine_report
from .personal import expand_aliases
from .privacy import describe as describe_privacy
from .privacy import indexed_sensitive, purge_indexed
from .query import Range, understand
from .search import search_page, similar_images, visual_text_search
from .store import Store, VectorCache
from .watch import Watcher

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
KIND_MAP = {
    "docs": ("text", "code", "pdf", "docx", "pptx", "xlsx", "epub", "odt", "rtf"),
    "text": ("text",),
    "code": ("code",),
    "pdf": ("pdf",),
    "docx": ("docx",),
    "image": ("image",),
}

# A wallpaper is a picture, not a photo library: 12 MB covers a 4K JPEG and
# stops a 200 MB TIFF from being pasted into the window and into the config dir.
BACKGROUND_MAX = 12 * 1024 * 1024
BACKGROUND_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
}
BACKGROUND_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif")


class IndexJob:
    """One indexing run at a time, with progress the UI can poll."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.position = 0
        self.total = 0
        self.status = ""
        self.path = ""
        self.started_at = 0.0
        self.finished_at = 0.0
        self.result: dict | None = None
        self.error: str | None = None
        self.thread: threading.Thread | None = None

    def snapshot(self) -> dict:
        elapsed = (self.finished_at or time.time()) - self.started_at if self.started_at else 0.0
        return {
            "running": self.running,
            "position": self.position,
            "total": self.total,
            "status": self.status,
            "path": self.path,
            "elapsed": round(elapsed, 1),
            "result": self.result,
            "error": self.error,
        }

    def start(self, roots: list[str], config: IndexConfig, db_path: Path, prune: bool) -> bool:
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.position = 0
            self.total = 0
            self.status = "starting"
            self.path = ""
            self.started_at = time.time()
            self.finished_at = 0.0
            self.result = None
            self.error = None
            self.thread = threading.Thread(
                target=self._run, args=(roots, config, db_path, prune), daemon=True
            )
            self.thread.start()
            return True

    def _run(self, roots: list[str], config: IndexConfig, db_path: Path, prune: bool) -> None:
        store = None
        try:
            embedder = get_embedder("auto", config.model_dir)
            visual = get_visual_embedder(config.model_dir)
            store = Store(db_path, embedder.name, embedder.dim)

            def progress(position: int, total: int, status: str, path: str) -> None:
                self.position = position
                self.total = total
                self.status = status
                self.path = path

            stats: IndexStats = run_index(
                roots, config, store, embedder, visual, prune=prune, progress=progress
            )
            self.result = stats.as_dict()
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
        finally:
            if store is not None:
                store.close()
            self.running = False
            self.finished_at = time.time()


JOB = IndexJob()
WATCHER: Watcher | None = None
WATCH_LOCK = threading.Lock()
CACHE = VectorCache()
# What the boot screen polls. The server answers requests from the first
# millisecond, but "ready" means the vector pack has been faulted in — that is
# the difference between a window that opens and a window that can search.
READY: dict = {
    "ready": False,
    "stage": "boot",
    "warm_ms": None,
    "at": None,
    "error": None,
    "started": time.time(),
}
# Set by serve(): the MCP handler keeps a warm Store between tool calls, and the
# token is read once at startup rather than per request.
MCP = None
MCP_TOKEN = ""


def _mcp_token_ok(header: str | None) -> bool:
    """Constant-time bearer check against the token on disk."""
    expected = MCP_TOKEN
    if not expected or not header:
        return False
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return False
    return secrets.compare_digest(value.strip(), expected)


class Handler(BaseHTTPRequestHandler):
    server_version = "SemFind/0.1"
    config: IndexConfig
    db_path: Path
    # Set by the desktop app so a second launch can raise the existing window
    # instead of starting a rival instance. None in plain browser mode.
    focus_hook = None

    # ------------------------------------------------------------- plumbing

    def log_message(self, fmt: str, *args) -> None:  # quieter console
        if os.environ.get("SEMFIND_VERBOSE"):
            super().log_message(fmt, *args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ALLOWED_HOSTS

    def _send(self, status: int, body: bytes, content_type: str = "application/json; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _error(self, message: str, status: int = 400) -> None:
        self._json({"error": message}, status)

    def _store(self, readonly: bool = True) -> Store:
        embedder = get_embedder("auto", self.config.model_dir)
        return Store(self.db_path, embedder.name, embedder.dim, readonly=readonly, cache=CACHE, vector_backend=self.config.vector_backend)

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._error("bad host", 403)
            return
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        try:
            if route == "/mcp":
                # The MCP transport is POST-only; a GET here is usually a client
                # probing for the legacy SSE endpoint.
                self._json({
                    "error": "use POST for JSON-RPC",
                    "transport": "streamable-http",
                    "endpoint": "/mcp",
                }, 405)
            elif route.startswith("/api/"):
                self._api_get(route, query)
            else:
                self._static(route)
        except Exception as exc:
            self._error(f"{type(exc).__name__}: {exc}", 500)

    def _api_get(self, route: str, query: dict) -> None:
        if route == "/api/search":
            self._search(query)
        elif route == "/api/stats":
            self._stats()
        elif route == "/api/status":
            self._status()
        elif route == "/api/progress":
            self._json(JOB.snapshot())
        elif route == "/api/ready":
            self._json(
                {
                    **READY,
                    "uptime_ms": round((time.time() - READY["started"]) * 1000.0, 1),
                }
            )
        elif route == "/api/ocr":
            self._json(engine_report(self.config.ocr_languages))
        elif route == "/api/thumb":
            self._thumb(query)
        elif route == "/api/preview":
            self._preview(query)
        elif route == "/api/similar":
            self._similar(query)
        elif route == "/api/images":
            self._images(query)
        elif route == "/api/watch":
            self._json(WATCHER.state.as_dict() if WATCHER else {"running": False})
        elif route == "/api/privacy":
            self._privacy()
        elif route == "/api/personal":
            self._personal()
        elif route == "/api/background":
            self._background()
        elif route == "/api/pins":
            self._json({"pins": self._pins()})
        elif route == "/api/saved":
            self._json({"saved": self._saved()})
        elif route == "/api/aliases":
            self._json({"aliases": self._aliases()})
        elif route == "/api/symbols":
            self._symbols(query)
        elif route in ("/api/open", "/api/reveal"):
            self._open_file(query, reveal=route.endswith("reveal"))
        else:
            self._error("unknown route", 404)

    def _search(self, query: dict) -> None:
        text = (query.get("q") or [""])[0].strip()
        if not text:
            self._json({"query": "", "ms": 0, "hits": [], "images": []})
            return
        limit = min(int((query.get("limit") or ["12"])[0]), 50)
        offset = max(int((query.get("offset") or ["0"])[0]), 0)
        kind_key = (query.get("kind") or [""])[0]
        folder = (query.get("folder") or [""])[0] or None
        since = (query.get("since") or [""])[0]
        sort = (query.get("sort") or [""])[0] or None

        parsed = understand(text)
        kinds = KIND_MAP.get(kind_key)
        if kinds:
            parsed.kinds |= set(kinds)
        if folder:
            parsed.in_paths.append(folder)
        since_ts = None
        if since:
            try:
                since_ts = time.mktime(time.strptime(since, "%Y-%m-%d"))
            except ValueError:
                since_ts = None
        if since_ts is not None:
            parsed.modified = Range(since_ts, None)

        embedder = get_embedder("auto", self.config.model_dir)
        store = self._store()
        started = time.perf_counter()
        try:
            # The dictionary is expanded before parsing, so `cv` reaches the
            # parser as `резюме` and every filter the UI added stays intact.
            expanded, applied = expand_aliases(store, text)
            if applied:
                parsed = understand(expanded)
                if kinds:
                    parsed.kinds |= set(kinds)
                if folder:
                    parsed.in_paths.append(folder)
                if since_ts is not None:
                    parsed.modified = Range(since_ts, None)
            page = search_page(
                store, embedder, expanded, limit=limit, offset=offset, sort=sort,
                parsed=parsed, usage=store.usage_map(), applied=applied,
            )
            # Visual text search only makes sense when the query still has free
            # text; a pure filter query would send nonsense to the image model.
            visual = get_visual_embedder(self.config.model_dir)
            images = (
                visual_text_search(store, visual, parsed.text or text, limit=8)
                if getattr(visual, "supports_text", False) and parsed.text
                else []
            )
        finally:
            store.close()
        elapsed = (time.perf_counter() - started) * 1000

        self._json(
            {
                "query": text,
                "ms": round(elapsed, 1),
                "total": page.total,
                "approx": page.approx,
                "offset": page.offset,
                "next_offset": page.next_offset,
                "sort": page.sort,
                "mode": page.mode,
                "chips": page.chips,
                "hints": page.hints,
                "facets": page.facets,
                "groups": page.groups,
                "collapsed": page.collapsed,
                "hits": [
                    {
                        "file_id": hit.file_id,
                        "path": hit.path,
                        "name": hit.name,
                        "kind": hit.kind,
                        "score": hit.score,
                        "snippet": hit.snippet,
                        "channels": list(hit.channels),
                        "mtime": hit.mtime,
                        "size": hit.size,
                        "is_image": hit.is_image,
                        "width": hit.width,
                        "height": hit.height,
                        "group_size": hit.group_size,
                        "group_reason": hit.group_reason,
                        "group_members": list(hit.group_members),
                        "pinned": hit.pinned,
                        "symbol": hit.symbol,
                        "thumb": f"/api/thumb?id={hit.file_id}" if hit.is_image else None,
                    }
                    for hit in page.hits
                ],
                "images": [
                    {
                        "file_id": image.file_id,
                        "path": image.path,
                        "name": image.name,
                        "score": image.score,
                        "thumb": f"/api/thumb?id={image.file_id}",
                    }
                    for image in images
                ],
            }
        )

    def _stats(self) -> None:
        store = self._store()
        try:
            stats = store.stats()
        finally:
            store.close()
        self._json({"db": str(self.db_path), **stats})

    def _status(self) -> None:
        text_embedder = get_embedder("auto", self.config.model_dir)
        visual = get_visual_embedder(self.config.model_dir)
        self._json(
            {
                "text_embedder": text_embedder.name,
                "visual_embedder": visual.name,
                "visual_supports_text": bool(getattr(visual, "supports_text", False)),
                "ocr": engine_report(self.config.ocr_languages),
                "privacy": {"enabled": self.config.privacy, "patterns": len(describe_privacy()["sensitive_dirs"])},
                "watching": bool(WATCHER and WATCHER.state.running),
                "db": str(self.db_path),
                "default_roots": _default_roots(),
                "version": "0.1.0",
            }
        )

    # ------------------------------------------------------------- background

    def _background_file(self) -> Path | None:
        for extension in BACKGROUND_EXTENSIONS:
            candidate = self.config.home / f"background{extension}"
            if candidate.is_file():
                return candidate
        return None

    def _background(self) -> None:
        """The chosen wallpaper, served from the config directory.

        It lives next to the index rather than in the browser's storage: a 4 MB
        photo in `localStorage` is a quota error waiting to happen, and the file
        then travels with a backup of `~/.semfind` instead of dying with a cache
        clear.
        """
        path = self._background_file()
        if path is None:
            self._error("no background", 404)
            return
        content_type, _ = mimetypes.guess_type(str(path))
        self._send(200, path.read_bytes(), content_type or "image/png")

    def _set_background(self, payload: dict) -> None:
        if payload.get("clear"):
            for extension in BACKGROUND_EXTENSIONS:
                (self.config.home / f"background{extension}").unlink(missing_ok=True)
            self._json({"cleared": True})
            return

        data = str(payload.get("data") or "")
        match = re.match(r"^data:(image/[a-z0-9.+-]+);base64,(.+)$", data, re.S | re.I)
        if not match:
            self._error("expected a base64 image data URL")
            return
        mime, encoded = match.group(1).lower(), match.group(2)
        extension = BACKGROUND_TYPES.get(mime)
        if extension is None:
            self._error(f"unsupported image type: {mime}")
            return
        try:
            raw = base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError):
            self._error("image data is not valid base64")
            return
        if not raw:
            self._error("empty image")
            return
        if len(raw) > BACKGROUND_MAX:
            self._error(f"image is larger than {BACKGROUND_MAX // 1048576} MB", 413)
            return

        self.config.ensure_home()
        # One background at a time: writing a new PNG over an old JPG would leave
        # both on disk and the reader picking whichever extension it tries first.
        for other in BACKGROUND_EXTENSIONS:
            (self.config.home / f"background{other}").unlink(missing_ok=True)
        target = self.config.home / f"background{extension}"
        temporary = target.with_suffix(target.suffix + ".part")
        temporary.write_bytes(raw)
        temporary.replace(target)
        self._json({"url": f"/api/background?v={int(time.time() * 1000)}", "bytes": len(raw)})

    # ------------------------------------------------------------- personal

    def _personal(self) -> None:
        """Everything the empty screen needs, in one round trip.

        The idle screen shows pinned files, saved searches and the dictionary at
        once. Three requests would open the store three times and pay the
        embedder's lazy load three times, which is the difference between a
        screen that appears and one that fades in.
        """
        self._json(
            {
                "pins": self._pins(),
                "saved": self._saved(),
                "aliases": self._aliases(),
                "symbols": self._symbol_count(),
            }
        )

    def _pins(self) -> list[dict]:
        store = self._store()
        try:
            rows = store.pinned_rows()
        finally:
            store.close()
        return [
            {
                "file_id": int(row["id"]),
                "path": row["path"],
                "name": row["name"],
                "kind": row["kind"],
                "size": int(row["size"] or 0),
                "mtime": float(row["mtime"] or 0.0),
                "added_at": float(row["added_at"] or 0.0),
                "is_image": row["kind"] == "image",
                "thumb": f"/api/thumb?id={row['id']}" if row["kind"] == "image" else None,
            }
            for row in rows
        ]

    def _saved(self) -> list[dict]:
        store = self._store()
        try:
            rows = store.saved_searches()
        finally:
            store.close()
        return [
            {
                "id": int(row["id"]),
                "query": row["query"],
                "label": row["label"] or row["query"],
                "created": float(row["created"] or 0.0),
                "used": int(row["used"] or 0),
                "last_used": float(row["last_used"] or 0.0),
            }
            for row in rows
        ]

    def _aliases(self) -> list[dict]:
        store = self._store()
        try:
            table = store.aliases()
        finally:
            store.close()
        return [{"alias": alias, "expand": expand} for alias, expand in sorted(table.items())]

    def _symbol_count(self) -> int:
        store = self._store()
        try:
            return store.symbol_count()
        finally:
            store.close()

    def _symbols(self, query: dict) -> None:
        needle = (query.get("q") or [""])[0].strip()
        limit = min(int((query.get("limit") or ["40"])[0]), 200)
        store = self._store()
        try:
            rows = store.find_symbols(needle, limit) if needle else []
        finally:
            store.close()
        self._json(
            {
                "query": needle,
                "symbols": [
                    {
                        "name": row["name"],
                        "kind": row["kind"],
                        "line": int(row["line"]),
                        "text": row["text"],
                        "file_id": int(row["file_id"]),
                        "path": row["path"],
                    }
                    for row in rows
                ],
            }
        )

    def _write(self, action):
        """Run one write against the index and commit it.

        Pins, saved searches and dictionary entries are the user's own data, so
        they are committed immediately: a crash two seconds after the star was
        clicked must not lose the star.
        """
        store = self._store(readonly=False)
        try:
            result = action(store)
            store.commit()
            return result
        finally:
            store.close()

    def _pin(self, payload: dict) -> None:
        from .personal import pin

        file_id = int(payload.get("id") or 0)
        if not file_id:
            self._error("id required")
            return
        # No `pinned` key means toggle, which is what the star does.
        wanted = payload.get("pinned")
        if wanted is None:
            store = self._store()
            try:
                wanted = file_id not in store.pinned_ids()
            finally:
                store.close()
        state = self._write(lambda store: pin(store, file_id, bool(wanted)))
        self._json({"id": file_id, "pinned": state})

    def _save_search(self, payload: dict) -> None:
        from .personal import save

        query = str(payload.get("query") or "").strip()
        if not query:
            self._error("query required")
            return
        label = str(payload.get("label") or "").strip()
        self._json(self._write(lambda store: save(store, query, label)))

    def _drop_search(self, payload: dict) -> None:
        from .personal import drop

        saved_id = int(payload.get("id") or 0)
        if not saved_id:
            self._error("id required")
            return
        self._json({"dropped": self._write(lambda store: drop(store, saved_id))})

    def _set_alias(self, payload: dict) -> None:
        from .personal import teach

        alias = str(payload.get("alias") or "")
        expand = str(payload.get("expand") or "")
        try:
            self._json(self._write(lambda store: teach(store, alias, expand)))
        except ValueError as exc:
            self._error(str(exc))

    def _drop_alias(self, payload: dict) -> None:
        from .personal import forget

        alias = str(payload.get("alias") or "")
        if not alias:
            self._error("alias required")
            return
        self._json({"dropped": self._write(lambda store: forget(store, alias))})

    def _thumb(self, query: dict) -> None:
        file_id = int((query.get("id") or ["0"])[0])
        store = self._store()
        try:
            data = store.thumb(file_id)
        finally:
            store.close()
        if not data:
            self._error("no thumbnail", 404)
            return
        self._send(200, data, "image/jpeg")

    def _images(self, query: dict) -> None:
        limit = min(int((query.get("limit") or ["60"])[0]), 200)
        offset = int((query.get("offset") or ["0"])[0])
        store = self._store()
        try:
            rows = store.image_rows(limit, offset)
        finally:
            store.close()
        self._json(
            {
                "images": [
                    {
                        "file_id": row["file_id"],
                        "path": row["path"],
                        "name": row["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                        "width": row["width"],
                        "height": row["height"],
                        "thumb": f"/api/thumb?id={row['file_id']}",
                        "ocr": (row["ocr_text"] or "")[:180],
                    }
                    for row in rows
                    if row["file_id"] is not None
                ]
            }
        )

    def _similar(self, query: dict) -> None:
        file_id = int((query.get("id") or ["0"])[0])
        visual = get_visual_embedder(self.config.model_dir)
        store = self._store()
        try:
            hits = similar_images(store, visual, file_id, limit=12)
        finally:
            store.close()
        self._json(
            {
                "images": [
                    {
                        "file_id": hit.file_id,
                        "path": hit.path,
                        "name": hit.name,
                        "score": hit.score,
                        "thumb": f"/api/thumb?id={hit.file_id}",
                    }
                    for hit in hits
                ]
            }
        )

    def _privacy(self) -> None:
        store = self._store()
        try:
            found = indexed_sensitive(store, limit=50)
        finally:
            store.close()
        self._json(
            {
                "enabled": self.config.privacy,
                "patterns": len(describe_privacy()["sensitive_dirs"]),
                "count": len(found),
                "files": found,
            }
        )

    def _preview(self, query: dict) -> None:
        """Quick Look: the first chunks of a file, plus image geometry.

        The index already holds extracted text for every kind, so a PDF or DOCX
        preview costs one query instead of re-parsing the file. Images get their
        thumbnail and OCR text instead of a body.
        """
        file_id = int((query.get("id") or ["0"])[0])
        limit = min(int((query.get("chars") or ["1600"])[0]), 8000)
        store = self._store()
        try:
            row = store.file_by_id(file_id)
            if row is None:
                self._error("unknown file", 404)
                return
            chunks = store.conn.execute(
                "SELECT text FROM chunks WHERE file_id = ? ORDER BY ord LIMIT 4", (file_id,)
            ).fetchall()
            image = store.image_by_file_id(file_id) if row["kind"] == "image" else None
            usage = store.conn.execute(
                "SELECT opens FROM usage WHERE file_id = ?", (file_id,)
            ).fetchone()
        finally:
            store.close()

        text = "\n".join(chunk["text"] for chunk in chunks)[:limit]
        self._json(
            {
                "file_id": file_id,
                "path": row["path"],
                "name": row["path"].rsplit("\\", 1)[-1].rsplit("/", 1)[-1],
                "kind": row["kind"],
                "size": row["size"],
                "mtime": row["mtime"],
                "opens": int(usage["opens"]) if usage else 0,
                "exists": Path(row["path"]).exists(),
                "text": text,
                "truncated": len(text) >= limit,
                "image": None
                if image is None
                else {
                    "width": image["width"],
                    "height": image["height"],
                    "thumb": f"/api/thumb?id={file_id}",
                    "ocr": (image["ocr_text"] or "")[:limit],
                },
            }
        )

    def _open_file(self, query: dict, reveal: bool) -> None:
        file_id = int((query.get("id") or ["0"])[0])
        store = self._store()
        try:
            row = store.file_by_id(file_id)
        finally:
            store.close()
        if row is None:
            self._error("unknown file", 404)
            return
        path = row["path"]
        if not Path(path).exists():
            self._error("file is gone from disk", 410)
            return
        try:
            if sys.platform == "win32":
                if reveal:
                    subprocess.Popen(["explorer", "/select,", str(Path(path))])
                else:
                    os.startfile(path)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
            else:
                subprocess.Popen(["xdg-open", str(Path(path).parent if reveal else path)])
        except OSError as exc:
            self._error(f"cannot open: {exc}", 500)
            return
        # Frecency is fed by real opens only, so "show in folder" must not count:
        # a file you reveal to delete it is not a file you care about.
        if not reveal:
            store = self._store()
            try:
                store.record_open(file_id)
                store.commit()
            except Exception:
                pass  # a read-only index must not fail the open
            finally:
                store.close()
        self._json({"ok": True})

    def _static(self, route: str) -> None:
        relative = "index.html" if route in ("/", "") else unquote(route.lstrip("/"))
        target = (WEB_DIR / relative).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self._error("not found", 404)
            return
        content_type, _ = mimetypes.guess_type(str(target))
        self._send(200, target.read_bytes(), content_type or "application/octet-stream")

    # ----------------------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._error("bad host", 403)
            return
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._error("invalid json")
            return

        if parsed.path == "/api/index":
            roots = payload.get("roots") or []
            if not roots:
                self._error("roots required")
                return
            prune = bool(payload.get("prune"))
            started = JOB.start(roots, self.config, self.db_path, prune)
            self._json({"started": started}, 202 if started else 409)
        elif parsed.path == "/mcp":
            self._mcp(payload)
        elif parsed.path == "/api/reindex":
            self._reindex()
        elif parsed.path == "/api/watch":
            self._watch(payload)
        elif parsed.path == "/api/privacy/purge":
            self._purge()
        elif parsed.path == "/api/pin":
            self._pin(payload)
        elif parsed.path == "/api/saved":
            self._save_search(payload)
        elif parsed.path == "/api/saved/drop":
            self._drop_search(payload)
        elif parsed.path == "/api/alias":
            self._set_alias(payload)
        elif parsed.path == "/api/alias/drop":
            self._drop_alias(payload)
        elif parsed.path == "/api/background":
            self._set_background(payload)
        elif parsed.path == "/api/focus":
            # A second launch asks the running app to come forward instead of
            # starting a rival instance with its own port and its own index.
            if Handler.focus_hook is None:
                self._error("not running as an app", 404)
            else:
                Handler.focus_hook()
                self._json({"focused": True})
        else:
            self._error("unknown route", 404)

    # ------------------------------------------------------------------- MCP

    def _mcp(self, payload) -> None:
        """Streamable-HTTP transport for the MCP server.

        The same JSON-RPC handler as `semfind mcp`, served on the port the app
        already listens on. A bearer token is required because binding to
        127.0.0.1 is not a security boundary on a machine with a browser: any
        page can POST to localhost, and the index is the user's whole disk.
        """
        if not _mcp_token_ok(self.headers.get("Authorization")):
            self._json({"error": "unauthorized"}, 401)
            return
        if MCP is None:
            self._json({"error": "mcp not enabled"}, 503)
            return

        messages = payload if isinstance(payload, list) else [payload]
        responses = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            try:
                response = MCP.handle(message)
            except Exception as exc:  # a bad call must not drop the session
                response = {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
                }
            if response is not None:
                responses.append(response)

        if not responses:
            # Notifications get 202 and no body, per the transport spec.
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(responses if isinstance(payload, list) else responses[0],
                          ensure_ascii=False).encode("utf-8")
        accept = (self.headers.get("Accept") or "").lower()
        if "text/event-stream" in accept:
            # Some clients only speak SSE; one event, then close.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body) + 8))
            self.end_headers()
            self.wfile.write(b"data: " + body + b"\n\n")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _watch(self, payload: dict) -> None:
        global WATCHER
        if not payload.get("enabled", True):
            with WATCH_LOCK:
                if WATCHER is not None:
                    WATCHER.stop()
                    WATCHER = None
            self._json({"running": False, "roots": []})
            return

        roots = [str(root) for root in (payload.get("roots") or []) if str(root).strip()]
        if not roots:
            roots = _default_roots()
        if not roots:
            self._error("no folders to watch")
            return
        wanted = [str(Path(root).expanduser()) for root in roots]

        with WATCH_LOCK:
            if WATCHER is not None and WATCHER.state.running and WATCHER.roots == wanted:
                self._json(WATCHER.state.as_dict())
                return
            if WATCHER is not None:
                WATCHER.stop()
            WATCHER = Watcher(wanted, self.config, self.db_path, pause_when=lambda: JOB.running)
            WATCHER.start()
            snapshot = WATCHER.state.as_dict()
        self._json(snapshot)

    def _purge(self) -> None:
        embedder = get_embedder("auto", self.config.model_dir)
        store = Store(self.db_path, embedder.name, embedder.dim, vector_backend=self.config.vector_backend)
        try:
            result = purge_indexed(store)
        finally:
            store.close()
        self._json(result)

    def _reindex(self) -> None:
        store = self._store()
        try:
            rows = store.conn.execute("SELECT DISTINCT path FROM files").fetchall()
        finally:
            store.close()
        roots = sorted({str(Path(row["path"]).parent) for row in rows})
        if not roots:
            self._error("index is empty", 409)
            return
        started = JOB.start(roots, self.config, self.db_path, False)
        self._json({"started": started, "roots": roots}, 202 if started else 409)


def _default_roots() -> list[str]:
    """Folders worth indexing on a fresh install: documents, downloads, screenshots."""
    home = Path.home()
    candidates = [
        home / "Documents",
        home / "Downloads",
        home / "Desktop",
        home / "Pictures" / "Screenshots",
        home / "Pictures",
    ]
    return [str(path) for path in candidates if path.is_dir()]


def start_server(
    db_path: Path,
    config: IndexConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> tuple[ThreadingHTTPServer, str]:
    """Bind the UI server without serving yet, and return it with its URL.

    Split out from `serve()` so the desktop window can run the same server on a
    background thread and shut it down when the window closes. Two processes
    would mean two ports, two caches and two copies of the index open — the
    window must own the server, not sit next to it.
    """
    global MCP, MCP_TOKEN
    Handler.config = config
    Handler.db_path = db_path
    READY.update(
        {
            "ready": False,
            "stage": "boot",
            "warm_ms": None,
            "at": None,
            "error": None,
            "started": time.time(),
        }
    )

    server = None
    for candidate in range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError("no free port in range")

    MCP = McpServer(db_path, config)
    MCP_TOKEN = mcp_token(config)
    return server, f"http://{host}:{port}/"


def prewarm(db_path: Path, config: IndexConfig) -> None:
    """Touch the index in the background so the first search is not the slow one."""
    threading.Thread(
        target=_prewarm, args=(db_path, config), name="semfind-prewarm", daemon=True
    ).start()


def serve(
    db_path: Path,
    config: IndexConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
) -> None:
    server, url = start_server(db_path, config, host=host, port=port)
    # flush: when stdout is a log file or a pipe, block buffering would hide
    # the address until the process exits — which is exactly when nobody needs
    # to know it any more.
    print(f"SemFind UI  ->  {url}", flush=True)
    print(f"index       ->  {db_path}", flush=True)
    print(
        f"mcp (http)  ->  {url}mcp  "
        f"(bearer token in {config.home / 'mcp_token'})",
        flush=True,
    )
    prewarm(db_path, config)
    if open_browser:
        threading.Timer(0.6, lambda: _open_browser(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.server_close()
        if MCP is not None:
            MCP.close()


def _prewarm(db_path: Path, config: IndexConfig) -> None:
    """Touch the vector store before the first user query.

    With the pack backend this is an mmap plus one warm-up scan, not a 2.6 s
    matrix build: the point is to fault the pages in on a background thread
    instead of on the first keystroke.
    """
    started = time.perf_counter()
    READY["stage"] = "vectors"
    try:
        embedder = get_embedder("auto", config.model_dir)
        store = Store(db_path, embedder.name, embedder.dim, readonly=True,
                      cache=CACHE, vector_backend=config.vector_backend)
        try:
            if store.vector_backend == "pack":
                pack = store.pack()
                if pack is not None:
                    pack.search(np.zeros(store.dim, dtype=np.float32), 1)
            else:
                store.vector_index()
        finally:
            store.close()
    except Exception as exc:
        READY["error"] = str(exc)[:200]
    finally:
        READY["stage"] = "ready"
        READY["warm_ms"] = round((time.perf_counter() - started) * 1000.0, 1)
        READY["at"] = time.time()
        READY["ready"] = True

    # After READY, not before: the window opens on time and this runs behind it.
    # An index built before the `symbols` table existed would otherwise answer
    # `sym:` with nothing until the next re-index, which looks like a broken
    # feature rather than a migration that has not run yet.
    _backfill_symbols(db_path, config)


def _backfill_symbols(db_path: Path, config: IndexConfig) -> None:
    from .indexer import backfill_symbols

    try:
        embedder = get_embedder("auto", config.model_dir)
        store = Store(
            db_path, embedder.name, embedder.dim,
            readonly=False, vector_backend=config.vector_backend,
        )
        try:
            if store.get_meta("symbols_backfilled"):
                return
            backfill_symbols(store)
            store.commit()
        finally:
            store.close()
    except Exception:
        pass  # a migration must never be the reason the app does not open


def _open_browser(url: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(url)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url])
    except OSError:
        pass
