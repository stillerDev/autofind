"""MCP server: let an AI agent search this machine's index.

Speaks JSON-RPC 2.0 over stdio, one JSON object per line, which is what Claude
Desktop, Cursor and the rest of the MCP clients expect from a stdio server.
Read-only by design: every tool here only reads the index, and `open`/`reveal`
are deliberately absent — an agent may look, not act.

The shape of the surface is borrowed from kensaku (MIT): a search with a cursor,
a bounded snippet reader, index state, and a regex escape hatch. Snippets are
size-budgeted so a tool call can never flood a model's context.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

from .config import IndexConfig
from .embed import get_embedder
from .personal import expand_aliases
from .query import understand
from .search import search_page
from .store import Store

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "autofind"
DEFAULT_PAGE = 10
MAX_PAGE = 50


def _text(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}]}


def _error(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _encode_cursor(offset: int) -> str:
    return f"o{offset}"


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    match = re.fullmatch(r"o(\d{1,7})", str(cursor))
    return int(match.group(1)) if match else 0


TOOLS = [
    {
        "name": "search",
        "description": (
            "Search the local file index (names, paths and document text). Read-only. "
            "Understands the autofind query grammar: type:, ext:, in:, path:, name:, "
            "content:, modified:<7d, size:>2mb, re:\"...\", sym:parse_query, -exclude, "
            "mode:exhaustive, sort:newest. Plain Russian or English phrasing also works "
            "(\"скриншоты за прошлую неделю\"). Returns ranked hits with a snippet and "
            "a cursor for the next page."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "page_size": {
                    "type": "integer", "minimum": 1, "maximum": MAX_PAGE, "default": DEFAULT_PAGE,
                    "description": "Results per page.",
                },
                "cursor": {"type": "string", "description": "next_cursor from the previous page."},
                "sort": {
                    "type": "string", "enum": ["best", "newest", "oldest", "largest", "name"],
                    "description": "Result order. Default: best match.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_snippet",
        "description": (
            "Read a bounded snippet from an indexed file, centred on the first match "
            "for an optional query. Never returns more than the token budget."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path of an indexed file."},
                "query": {"type": "string", "description": "Text to centre the snippet on."},
                "max_chars": {
                    "type": "integer", "minimum": 200, "maximum": 8000, "default": 1600,
                    "description": "Approximate character budget.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "index_state",
        "description": (
            "Report index size and freshness: file, chunk, image and vector counts, "
            "database size, embedder and OCR engine, and the age of the newest file."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "regex_search",
        "description": (
            "Bounded regular-expression search over indexed content. Read-only and "
            "slower than `search` (it scans), so keep the pattern narrow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Python regular expression."},
                "page_size": {
                    "type": "integer", "minimum": 1, "maximum": MAX_PAGE, "default": DEFAULT_PAGE,
                },
                "cursor": {"type": "string"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_symbol",
        "description": (
            "Find where a function, class, struct, type or method is defined in indexed "
            "code, rather than where the name appears. Exact name first, then prefix, "
            "then substring. Answers with file, line and the definition line itself — "
            "the cheapest way for an agent to jump to a definition without reading files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Symbol name, or a prefix of it."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "similar_images",
        "description": (
            "Find screenshots that look like a given indexed image (layout and colour). "
            "Useful for near-duplicates and for 'that screenshot from the same app'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path of an indexed image."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


class McpServer:
    def __init__(self, db_path: Path, config: IndexConfig) -> None:
        self.db_path = Path(db_path)
        self.config = config
        self._store: Store | None = None

    # ------------------------------------------------------------- plumbing

    def close(self) -> None:
        """Let go of the index. A read-only handle still blocks a delete on Windows."""
        if self._store is not None:
            self._store.close()
            self._store = None

    def _open(self) -> Store:
        if self._store is None:
            embedder = get_embedder("auto", self.config.model_dir)
            self._store = Store(self.db_path, embedder.name, embedder.dim, readonly=True, vector_backend=self.config.vector_backend)
        else:
            self._store.touch_check()
        return self._store

    def handle(self, request: dict) -> dict | None:
        method = request.get("method")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            return self._result(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": _version()},
                "instructions": (
                    "Local file search. Read-only: no tool here can modify anything. "
                    "Call tools/list to discover them."
                ),
            })
        if method in ("notifications/initialized", "initialized"):
            return None
        if method == "ping":
            return self._result(request_id, {})
        if method == "tools/list":
            return self._result(request_id, {"tools": TOOLS})
        if method == "tools/call":
            return self._result(request_id, self._call(params))
        if request_id is None:
            return None
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        }

    @staticmethod
    def _result(request_id, result) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ---------------------------------------------------------------- tools

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        handlers = {
            "search": self._search,
            "read_snippet": self._read_snippet,
            "index_state": self._index_state,
            "regex_search": self._regex_search,
            "similar_images": self._similar_images,
            "find_symbol": self._find_symbol,
        }
        handler = handlers.get(name)
        if handler is None:
            return _error(f"unknown tool: {name}")
        try:
            return handler(arguments)
        except Exception as exc:  # a tool failure must not kill the session
            return _error(f"{type(exc).__name__}: {exc}")

    def _search(self, arguments: dict) -> dict:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return _error("query is required")
        size = min(int(arguments.get("page_size") or DEFAULT_PAGE), MAX_PAGE)
        offset = _decode_cursor(arguments.get("cursor"))
        sort = arguments.get("sort") or None

        store = self._open()
        embedder = get_embedder("auto", self.config.model_dir)
        # The dictionary is expanded before parsing, so an alias means the same
        # thing here as it does in the window.
        query, applied = expand_aliases(store, query)
        page = search_page(
            store, embedder, query, limit=size, offset=offset, sort=sort,
            parsed=understand(query), usage=store.usage_map(), applied=applied,
        )
        next_offset = page.next_offset
        return _text({
            "query": query,
            # The label, not the raw token: for a dictionary hit the raw token is
            # the word the agent typed, and the agent needs to see what it became.
            "interpreted_as": [chip.get("label") or chip["raw"] for chip in page.chips],
            "total": page.total,
            "total_is_approximate": page.approx,
            "next_cursor": _encode_cursor(next_offset) if next_offset is not None else None,
            "hits": [
                {
                    "path": hit.path,
                    "name": hit.name,
                    "kind": hit.kind,
                    "score": hit.score,
                    "modified": time.strftime("%Y-%m-%d", time.localtime(hit.mtime)),
                    "size": hit.size,
                    "matched_by": list(hit.channels),
                    "snippet": hit.snippet,
                }
                for hit in page.hits
            ],
        })

    def _read_snippet(self, arguments: dict) -> dict:
        path = str(arguments.get("path") or "")
        query = str(arguments.get("query") or "")
        budget = min(int(arguments.get("max_chars") or 1600), 8000)
        if not path:
            return _error("path is required")
        store = self._open()
        row = store.get_file(path)
        if row is None:
            return _error(f"not in the index: {path}")
        body = store.file_text(int(row["id"]))
        if query:
            position = body.lower().find(query.lower())
            if position != -1:
                start = max(position - budget // 3, 0)
                body = body[start:start + budget]
            else:
                body = body[:budget]
        else:
            body = body[:budget]
        return _text({
            "path": path,
            "modified": time.strftime("%Y-%m-%d %H:%M", time.localtime(float(row["mtime"]))),
            "size": int(row["size"]),
            "text": body,
            "truncated": len(body) >= budget,
        })

    def _index_state(self, _arguments: dict) -> dict:
        store = self._open()
        stats = store.stats()
        newest = store.conn.execute("SELECT MAX(mtime) AS m FROM files").fetchone()
        embedder = get_embedder("auto", self.config.model_dir)
        return _text({
            "db": str(self.db_path),
            **stats,
            "embedder": embedder.name,
            "semantic": bool(getattr(embedder, "semantic", False)),
            "newest_file": (
                time.strftime("%Y-%m-%d %H:%M", time.localtime(float(newest["m"])))
                if newest and newest["m"] else None
            ),
            "index_age_seconds": (
                int(time.time() - float(newest["m"])) if newest and newest["m"] else None
            ),
        })

    def _find_symbol(self, arguments: dict) -> dict:
        name = str(arguments.get("name") or "").strip()
        if not name:
            return _error("name is required")
        limit = min(int(arguments.get("limit") or 20), 100)
        store = self._open()
        rows = store.find_symbols(name, limit)
        return _text({
            "name": name,
            "total": len(rows),
            "definitions": [
                {
                    "symbol": row["name"],
                    "kind": row["kind"],
                    "line": int(row["line"]),
                    "path": row["path"],
                    "source": row["text"],
                }
                for row in rows
            ],
        })

    def _regex_search(self, arguments: dict) -> dict:
        pattern = str(arguments.get("pattern") or "")
        if not pattern:
            return _error("pattern is required")
        size = min(int(arguments.get("page_size") or DEFAULT_PAGE), MAX_PAGE)
        offset = _decode_cursor(arguments.get("cursor"))
        try:
            re.compile(pattern)
        except re.error as exc:
            return _error(f"bad pattern: {exc}")

        store = self._open()
        rows = store.regex_chunks(pattern, offset + size + 1)
        window = rows[offset:offset + size]
        return _text({
            "pattern": pattern,
            "next_cursor": _encode_cursor(offset + size) if len(rows) > offset + size else None,
            "hits": [
                {
                    "path": row["path"],
                    "kind": row["kind"],
                    "snippet": (row["text"] or "")[:400],
                }
                for row in window
            ],
        })

    def _similar_images(self, arguments: dict) -> dict:
        from .search import similar_images

        path = str(arguments.get("path") or "")
        limit = min(int(arguments.get("limit") or 10), 30)
        if not path:
            return _error("path is required")
        store = self._open()
        row = store.get_file(path)
        if row is None:
            return _error(f"not in the index: {path}")
        hits = similar_images(store, None, int(row["id"]), limit=limit)
        return _text({
            "path": path,
            "hits": [
                {"path": hit.path, "score": hit.score, "width": hit.width, "height": hit.height}
                for hit in hits
            ],
        })

    # ----------------------------------------------------------------- loop

    def run(self) -> int:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                print(json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "parse error"},
                }), flush=True)
                continue

            response = self.handle(request)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)

        if self._store is not None:
            self._store.close()
        return 0


def _version() -> str:
    try:
        from importlib.metadata import version

        return version("autofind")
    except Exception:
        return "0.1.0"


def serve(db_path: Path, config: IndexConfig) -> int:
    return McpServer(db_path, config).run()
