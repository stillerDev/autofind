"""OpenAPI 3.0 description of the local REST surface.

Generated from one static table rather than per-handler decorators: the
handlers take raw `parse_qs` dicts and sqlite Rows, so the honest, useful
summary lives in this module where it can be read top to bottom. It is a
description, not a validator — the server keeps behaving exactly as before.

Served at `/api/openapi.json` so editors and MCP-style tools can point at
the running app and show typed request/response hints without touching
anything.
"""

from __future__ import annotations

from . import __version__

SERVER_URL = "http://127.0.0.1:8765"  # default; the served doc is port-aware

# (method, path, summary, query-params, body-fields) — GET bodies stay empty.
ROUTES: list[tuple[str, str, str, list[str], list[str]]] = [
    ("get", "/api/search", "Hybrid search over the index",
     ["q", "limit", "offset", "sort"], []),
    ("get", "/api/stats", "Index counters (files, chunks, images, vectors)", [], []),
    ("get", "/api/status", "App status: watcher, job, embedder, uptime", [], []),
    ("get", "/api/progress", "Snapshot of the running background job", [], []),
    ("get", "/api/ready", "Boot readiness probe", [], []),
    ("get", "/api/ocr", "Which OCR engines are available", [], []),
    ("get", "/api/log", "Tail of the server event log", ["lines"], []),
    ("get", "/api/onboarding", "Onboarding progress of this install", [], []),
    ("get", "/api/thumb", "Thumbnail of an indexed image", ["id", "size"], []),
    ("get", "/api/preview", "Text/content preview of one file", ["path"], []),
    ("get", "/api/similar", "Images that resemble the given one", ["path", "limit"], []),
    ("get", "/api/images", "Image-filtered result page", ["q", "limit", "offset"], []),
    ("get", "/api/watch", "Watchdog state and roots", [], []),
    ("get", "/api/privacy", "Privacy prefs: roots, exclusions, telemetry", [], []),
    ("get", "/api/seal", "Paranoid-mode state and seal progress", [], []),
    ("get", "/api/personal", "Learned aliases and file types", [], []),
    ("get", "/api/background", "Background indexing settings", [], []),
    ("get", "/api/pins", "Pinned results", [], []),
    ("get", "/api/saved", "Saved searches", [], []),
    ("get", "/api/aliases", "Personal dictionary aliases", [], []),
    ("get", "/api/types", "Custom file-type masks", [], []),
    ("get", "/api/tags", "Tags and their files", [], []),
    ("get", "/api/collections", "Saved collections", [], []),
    ("get", "/api/hotkeys", "Global hotkey configuration", [], []),
    ("get", "/api/audit", "Privacy audit of what the index stores", [], []),
    ("get", "/api/export", "Export search results", ["q", "format"], []),
    ("get", "/api/symbols", "Symbol definitions in indexed code", ["name", "limit"], []),
    ("get", "/api/open", "Open a file with its default app", ["path"], []),
    ("get", "/api/reveal", "Reveal a file in Explorer", ["path"], []),
    ("get", "/api/duplicates", "Duplicate-file report", [], []),
    ("get", "/api/openapi.json", "This document", [], []),
    ("post", "/api/reindex", "Re-crawl the configured roots", [], ["roots"]),
    ("post", "/api/watch", "Start/stop/reconfigure the watchdog", [], ["enabled", "roots"]),
    ("post", "/api/delete", "Permanently delete files and drop them from the index",
     [], ["ids"]),
    ("post", "/api/privacy", "Save privacy preferences", [], ["roots", "exclusions"]),
    ("post", "/api/seal", "Start sealing or unsealing the index", [], ["enabled"]),
    ("post", "/api/privacy/purge", "Wipe stored content for privacy", [], ["scope"]),
    ("post", "/api/pin", "Pin or unpin a result", [], ["path", "pinned"]),
    ("post", "/api/saved", "Save a search", [], ["name", "query"]),
    ("post", "/api/saved/drop", "Delete a saved search", [], ["id"]),
    ("post", "/api/alias", "Add a dictionary alias", [], ["word", "target"]),
    ("post", "/api/alias/drop", "Remove an alias", [], ["word"]),
    ("post", "/api/types", "Save custom file-type masks", [], ["name", "mask"]),
    ("post", "/api/tag", "Tag files", [], ["tag", "ids"]),
    ("post", "/api/collections", "Save a collection", [], ["name", "paths"]),
    ("post", "/api/hotkeys", "Save global hotkeys", [], []),
    ("post", "/api/background", "Save background-indexing settings", [], ["enabled", "idle_only"]),
    ("post", "/api/onboarding/done", "Mark onboarding finished", [], []),
    ("post", "/api/onboarding/reset", "Re-run onboarding next launch", [], []),
    ("post", "/api/autostart", "Enable or disable launch with Windows", [], ["enabled"]),
    ("post", "/api/import/kensaku", "Import a kensaku index", [], []),
    ("post", "/api/focus", "Bring the running window to the front", [], []),
    ("post", "/mcp", "MCP JSON-RPC over streamable HTTP (bearer token)", [], []),
]


def document(port: int) -> dict:
    """A plain-JSON OpenAPI 3.0 document for this running server."""
    paths: dict[str, dict] = {}
    for method, path, summary, params, _body in ROUTES:
        item = paths.setdefault(path, {})
        operation: dict = {
            "summary": summary,
            "operationId": f"{method}_{path.strip('/').replace('/', '_')}",
            "responses": {
                "200": {"description": "JSON response (see the app log for shapes)"},
            },
        }
        if params:
            operation["parameters"] = [
                {"name": name, "in": "query", "schema": {"type": "string"}}
                for name in params
            ]
        item[method] = operation
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "AutoFind local API",
            "version": __version__,
            "description": (
                "The REST surface the bundled UI talks to. Everything is "
                "bound to 127.0.0.1 and reads/writes the index of this "
                "machine only. POST /mcp needs a bearer token; every other "
                "route is loopback-trusted by design."
            ),
        },
        "servers": [{"url": f"http://127.0.0.1:{port}"}],
        "paths": paths,
    }
