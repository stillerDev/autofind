"""AutoFind launcher.

With no arguments it opens the desktop window: its own icon, its own tray, and
a global hotkey that brings it back while you are typing in another program.
With a subcommand it behaves exactly like `autofind`, so the same file works as a
double-click target, as an MCP entry point for an editor, and as a CLI:

    python run.py                       # the app window (tray + hotkey)
    python run.py --browser             # the same UI in a browser tab
    python run.py --no-browser --port 9000
    python run.py search "смета за март"
    python run.py mcp                   # JSON-RPC on stdio, for MCP clients

MCP clients get `python run.py --db ... mcp` from `autofind connect`; keeping the
dispatch here means that entry point does not depend on a console script being
on PATH.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import IndexConfig  # noqa: E402

# A token from this set anywhere in argv means "the caller wants the CLI".
COMMANDS = {
    "index", "search", "watch", "eval", "doctor", "privacy", "mcp",
    "cheat", "stats", "connect", "serve", "app", "autostart",
    "shortcut", "install", "ocr",
    # The personal layer and the migration: `run.py pin`, `run.py import kensaku`.
    "pin", "pins", "saved", "alias", "sym", "import",
}


def launcher_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="AutoFind: local search over files and screenshots",
        epilog="Subcommands: python run.py <index|search|app|autostart|...> (see `python run.py cheat`)",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="start the server without opening anything")
    parser.add_argument("--browser", action="store_true", help="use the browser instead of the app window")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--hotkey", default="ctrl+alt+space", help="show/hide the window")
    parser.add_argument("--no-hotkey", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--hidden", action="store_true", help="start in the tray (used at login)")
    parser.add_argument("--windowed", action="store_true",
                        help="framed, resizable window instead of borderless")
    parser.add_argument("--debug", action="store_true")
    return parser


def launcher_flags() -> set[str]:
    """Derived from the parser, never a second hand-kept list.

    A parallel list is how `--hidden` got added to the parser but not to the
    routing, so `run.py --hidden` fell through to the CLI and died on
    "the following arguments are required: command" — twice.
    """
    return {
        option
        for action in launcher_parser()._actions
        for option in action.option_strings
    }


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
    flags = launcher_flags()
    return not any(argument.split("=")[0] in flags for argument in argv)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if _is_cli(argv):
        from core.cli import main as cli_main

        return cli_main(argv)

    args = launcher_parser().parse_args(argv)

    config = IndexConfig()
    config.ensure_home()
    db_path = args.db or config.db_path

    if args.no_browser:
        from core.api import serve

        serve(db_path, config, port=args.port, open_browser=False)
        return 0

    from core.app import available, run_app

    if args.browser or not available():
        if not args.browser:
            print("pywebview is not installed; using the browser", file=sys.stderr)
        from core.api import serve

        serve(db_path, config, port=args.port, open_browser=True)
        return 0

    return run_app(
        db_path,
        config,
        port=args.port,
        hotkey=None if args.no_hotkey else args.hotkey,
        tray=not args.no_tray,
        start_hidden=args.hidden,
        borderless=not args.windowed,
        debug=args.debug,
    )


if __name__ == "__main__":
    raise SystemExit(main())
