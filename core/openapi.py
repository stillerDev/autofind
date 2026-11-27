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

from .__version__ import __version__

# (method, path, summary, query-params, body-fields) — GET bodies stay empty.
# Keep this in step with Handler._api_get / Handler._do_post: a route that is
# implemented but missing here is invisible to every OpenAPI-driven client.
ROUTES: list[tuple[str, str, str, list[str], list[str]]] = [
    ("get", "/api/search", "Hybrid search over the index",
     ["q", "limit", "offset", "sort", "kind", "folder", "since", "docdate"], []),
    ("get", "/api/stats", "Index counters (files, chunks, images, vectors)", [], []),
    ("get", "/api/status", "App status: watcher, job, embedder, uptime", [], []),
    ("get", "/api/progress", "Snapshot of the running background job", [], []),
    ("get", "/api/ready", "Boot readiness probe", [], []),
    ("get", "/api/ocr", "Which OCR engines are available", [], []),
    ("get", "/api/log", "Tail of the server event log", ["lines"], []),
    ("get", "/api/onboarding", "Onboarding progress of this install", [], []),
    ("get", "/api/import/kensaku", "Inspect a foreign kensaku install", [], []),
    ("get", "/api/thumb", "Thumbnail of an indexed image", ["id"], []),
    ("get", "/api/ocr-boxes", "OCR boxes matching a term on one image", ["id", "q"], []),
    ("get", "/api/preview", "Text/content preview of one file", ["id", "chars"], []),
    ("get", "/api/similar", "Images that resemble the given one", ["id", "limit"], []),
    ("get", "/api/images", "Image-filtered result page", ["q", "limit", "offset"], []),
    ("get", "/api/watch", "Watchdog state and roots", [], []),
    ("get", "/api/privacy", "Privacy prefs: roots, exclusions, telemetry", [], []),
    ("get", "/api/seal", "Paranoid-mode state and seal progress", [], []),
    ("get", "/api/self-test", "Bounded index integrity report", [], []),
    ("get", "/api/health", "Liveness, uptime, version, index freshness", [], []),
    ("get", "/api/personal", "Learned aliases and file types", [], []),
    ("get", "/api/background", "Background image", [], []),
    ("get", "/api/pins", "Pinned results", [], []),
    ("get", "/api/saved", "Saved searches", [], []),
    ("get", "/api/aliases", "Personal dictionary aliases", [], []),
    ("get", "/api/types", "Custom file-type masks", [], []),
    ("get", "/api/tags", "Tags and their files", [], []),
    ("get", "/api/notes", "Notes on a set of file ids", ["ids"], []),
    ("get", "/api/tag-colors", "Tag colour assignments", [], []),
    ("get", "/api/collections", "Saved collections", [], []),
    ("get", "/api/hotkeys", "Global hotkey configuration", [], []),
    ("get", "/api/audit", "Privacy audit of what the index stores", [], []),
    ("get", "/api/export", "Export search results", ["q", "format"], []),
    ("get", "/api/symbols", "Symbol definitions in indexed code", ["q", "limit"], []),
    ("get", "/api/open", "Open a file with its default app", ["id"], []),
    ("get", "/api/reveal", "Reveal a file in Explorer", ["id"], []),
    ("get", "/api/duplicates", "Duplicate-file report", ["mode"], []),
    ("get", "/api/duplicates/stream", "Streaming newline-delimited duplicate report",
     ["mode"], []),
    ("get", "/api/vault", "Vault mode, key presence and seal state (no secrets)", [], []),
    ("get", "/api/autostart", "Whether launch with Windows is enabled", [], []),
    ("get", "/api/rules", "Per-root indexing rules and global exclusions", [], []),
    ("get", "/api/exclusions", "Global exclusion patterns", [], []),
    ("get", "/api/backup", "Backup status and available archives", [], []),
    ("get", "/api/portable", "Portable-mode state and effective data directory", [], []),
    ("get", "/api/similar-files", "Files similar to the given file", ["id", "limit"], []),
    ("get", "/api/history", "Query history with usage context", ["limit"], []),
    ("get", "/api/recent", "Recently opened or indexed files for the menu", ["limit"], []),
    ("get", "/api/privacy/categories", "Privacy category switches", [], []),
    ("get", "/api/openapi.json", "This document", [], []),
    ("post", "/api/index", "Index the given roots", [], ["roots", "prune"]),
    ("post", "/api/reindex", "Re-crawl the roots of the current index", [], []),
    ("post", "/api/reindex-one", "Re-read one file", [], ["id"]),
    ("post", "/api/screenshot", "Index a saved screenshot", [], ["path"]),
    ("post", "/api/note", "Set a note on one file", [], ["id", "text"]),
    ("post", "/api/tag-color", "Set the colour of one tag", [], ["tag", "color"]),
    ("post", "/api/watch", "Start/stop/reconfigure the watchdog",
     [], ["enabled", "roots", "idle_only", "allow_remote"]),
    ("post", "/api/delete", "Permanently delete files and drop them from the index",
     [], ["ids"]),
    ("post", "/api/privacy", "Save privacy preferences", [], ["enabled", "categories"]),
    ("post", "/api/seal", "Start sealing or unsealing the index", [], ["action"]),
    ("post", "/api/privacy/purge", "Wipe stored content for privacy", [], []),
    ("post", "/api/pin", "Pin or unpin a result", [], ["ids", "pinned"]),
    ("post", "/api/saved", "Save a search", [], ["query", "label"]),
    ("post", "/api/saved/drop", "Delete a saved search", [], ["id"]),
    ("post", "/api/alias", "Add a dictionary alias", [], ["alias", "expand"]),
    ("post", "/api/alias/drop", "Remove an alias", [], ["alias"]),
    ("post", "/api/types", "Save custom file-type masks", [], ["types"]),
    ("post", "/api/tag", "Tag files", [], ["tag", "ids", "id", "on"]),
    ("post", "/api/collections", "Create/rename/drop a collection or edit its members",
     [], ["action", "id", "ids", "name"]),
    ("post", "/api/hotkeys", "Save global hotkeys", [], ["hotkeys"]),
    ("post", "/api/background", "Save/clear the background image",
     [], ["data", "clear", "enabled", "idle_only"]),
    ("post", "/api/onboarding/done", "Mark onboarding finished", [], []),
    ("post", "/api/onboarding/reset", "Re-run onboarding next launch", [], []),
    ("post", "/api/autostart", "Enable or disable launch with Windows", [], ["enabled"]),
    ("post", "/api/import/kensaku", "Import a kensaku index", [], []),
    ("post", "/api/vault/passphrase", "Set the vault passphrase (only when no key exists)",
     [], ["action", "passphrase"]),
    ("post", "/api/rules", "Save per-root indexing rules", [], ["roots", "exclusions"]),
    ("post", "/api/exclusions", "Save global exclusion patterns", [], ["exclusions", "patterns"]),
    ("post", "/api/backup", "Create an index backup archive", [], ["to", "destination"]),
    ("post", "/api/privacy/categories", "Save privacy category switches", [], ["enabled"]),
    ("post", "/api/reindex-folder", "Reindex the given folder(s)",
     [], ["folder", "folders", "prune"]),
    ("post", "/api/note/drop", "Clear a note", [], ["id"]),
    ("post", "/api/focus", "Bring the running window to the front", [], []),
    ("post", "/mcp", "MCP JSON-RPC over streamable HTTP (bearer token)", [], []),
]

# One table of codes the handlers really emit. Generated per verb so a new
# route cannot silently claim to return only 200.
_RESPONSE_CODES: dict[str, tuple[str, ...]] = {
    "get": ("200", "400", "403", "404", "500"),
    "post": ("200", "202", "400", "401", "403", "404", "409", "413", "415", "500", "503"),
}
_CODE_DESCRIPTIONS = {
    "200": "Success",
    "202": "Accepted — background work started or notification received",
    "400": "Invalid request",
    "401": "Bearer token missing or invalid",
    "403": "Bad Host/Origin or forbidden request",
    "404": "Unknown route or missing object",
    "409": "Conflicting state (job already running)",
    "413": "Payload too large",
    "415": "Content-Type must be application/json",
    "500": "Handler error",
    "503": "Service not enabled",
}


def document(port: int) -> dict:
    """A plain-JSON OpenAPI 3.0 document for this running server."""
    paths: dict[str, dict] = {}
    for method, path, summary, params, _body in ROUTES:
        item = paths.setdefault(path, {})
        operation: dict = {
            "summary": summary,
            "operationId": f"{method}_{path.strip('/').replace('/', '_')}",
            "responses": {
                code: {"description": _CODE_DESCRIPTIONS[code]}
                for code in _RESPONSE_CODES[method]
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
