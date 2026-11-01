"""SemFind launcher.

With no arguments it starts the local UI and opens it in the browser. With a
subcommand it behaves exactly like `semfind`, so the same file works as a
double-click launcher, as an MCP entry point for an editor, and as a CLI:

    python run.py                       # UI on the first free port from 8765
    python run.py --port 9000 --no-browser
    python run.py search "смета за март"
    python run.py mcp                   # JSON-RPC on stdio, for MCP clients

MCP clients get `python run.py --db ... mcp` from `semfind connect`; keeping the
dispatch here means that entry point does not depend on a console script being
on PATH.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import IndexConfig  # noqa: E402

# Anything that is not a launcher flag means the caller wants the CLI.
COMMANDS = {
    "index", "search", "watch", "eval", "doctor", "privacy", "mcp",
    "cheat", "stats", "connect",
}
LAUNCHER_FLAGS = {"--port", "--no-browser", "--db"}


def _is_cli(argv: list[str]) -> bool:
    # A bare `python run.py` is the launcher: double-click, F5, a shortcut.
    # Without this the empty list falls through to "no launcher flag, so the
    # CLI", and argparse dies with "the following arguments are required:
    # command" — a launcher must never fail that way.
    if not argv:
        return False
    if any(argument in COMMANDS for argument in argv):
        return True
    # `run.py --db x --help` is the launcher's own help.
    return not any(argument.split("=")[0] in LAUNCHER_FLAGS for argument in argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if _is_cli(argv):
        from core.cli import main as cli_main

        return cli_main(argv)

    import argparse

    from core.api import serve

    parser = argparse.ArgumentParser(description="SemFind local search")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args(argv)

    config = IndexConfig()
    config.ensure_home()
    db_path = args.db or config.db_path
    serve(db_path, config, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
