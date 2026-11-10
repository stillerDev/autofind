"""Wire autofind into MCP clients with one command.

`autofind mcp` speaks JSON-RPC over stdio, which every client supports but which
means the client owns the process and the index is only reachable while the
desktop app is open. The HTTP transport removes that: the same tools are served
by the running app on 127.0.0.1, guarded by a bearer token that is generated
once and kept out of the config files it is copied into.

Both are offered because the right answer depends on the client: Claude Desktop
and Cursor launch a stdio process happily, while anything running in a sandbox
(a browser-based agent, a container) can only reach an HTTP URL.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import IndexConfig

TOKEN_FILE = "mcp_token"
SERVER_KEY = "autofind"


@dataclass(slots=True)
class Target:
    """One client: where its config lives and how to talk to it."""

    name: str
    label: str
    path: Path
    note: str = ""


def token(config: IndexConfig) -> str:
    """Read or create the bearer token. Never printed unless asked for."""
    path = config.home / TOKEN_FILE
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    except OSError:
        pass
    config.ensure_home()
    value = secrets.token_urlsafe(32)
    path.write_text(value + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows: the profile directory is already user-only
    return value


def targets() -> list[Target]:
    home = Path.home()
    found: list[Target] = []
    if sys.platform == "win32":
        appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        found.append(Target(
            "claude-desktop", "Claude Desktop",
            appdata / "Claude" / "claude_desktop_config.json",
        ))
        found.append(Target(
            "cursor", "Cursor",
            home / ".cursor" / "mcp.json",
        ))
    else:
        found.append(Target(
            "claude-desktop", "Claude Desktop",
            home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
            if sys.platform == "darwin" else home / ".config" / "Claude" / "claude_desktop_config.json",
        ))
        found.append(Target("cursor", "Cursor", home / ".cursor" / "mcp.json"))
    found.append(Target(
        "vscode", "VS Code (workspace)", Path(".vscode") / "mcp.json",
        note="relative to the project you open",
    ))
    return found


def find(name: str) -> Target | None:
    for target in targets():
        if target.name == name:
            return target
    return None


def entry(transport: str, config: IndexConfig, port: int, db_path: Path) -> dict:
    """The `mcpServers` value for one client."""
    if transport == "http":
        return {
            "url": f"http://127.0.0.1:{port}/mcp",
            "headers": {"Authorization": f"Bearer {token(config)}"},
        }
    # stdio: the client starts the process itself, from a working directory we
    # do not control, so `python -m core.cli` would fail to import. Prefer the
    # installed console script; fall back to a one-liner that puts the project
    # root on sys.path explicitly.
    installed = shutil.which("autofind")
    if installed:
        # `which` can return a path relative to the current directory when "." is
        # on PATH; the client runs from somewhere else, so make it absolute.
        if os.sep in installed or (os.altsep and os.altsep in installed):
            installed = str(Path(installed).resolve())
        return {"command": installed, "args": ["--db", str(db_path), "mcp"]}
    root = Path(__file__).resolve().parent.parent
    bootstrap = (
        "import sys;"
        f"sys.path.insert(0, {str(root)!r});"
        "from core.cli import main;"
        "sys.exit(main())"
    )
    return {
        "command": sys.executable,
        "args": ["-c", bootstrap, "--db", str(db_path), "mcp"],
    }


def merge(existing: dict, value: dict) -> dict:
    """Add or replace our server without touching the user's other servers."""
    payload = dict(existing) if isinstance(existing, dict) else {}
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers[SERVER_KEY] = value
    payload["mcpServers"] = servers
    return payload


def write(target: Target, value: dict, dry_run: bool = False) -> tuple[bool, str]:
    """Merge our entry into the client config. Returns (changed, message)."""
    path = target.path
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"{path}: cannot read ({exc}); leaving it alone"
    payload = merge(existing, value)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if dry_run:
        return True, f"{path}\n{text}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_bytes(path.read_bytes())
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return False, f"{path}: {exc}"
    return True, str(path)


def snippet(transport: str, config: IndexConfig, port: int, db_path: Path) -> str:
    """What to paste if the client's config lives somewhere we do not know."""
    return json.dumps(
        {"mcpServers": {SERVER_KEY: entry(transport, config, port, db_path)}},
        ensure_ascii=False,
        indent=2,
    )
