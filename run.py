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
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core import DEFAULT_PORT  # noqa: E402
from core.config import IndexConfig  # noqa: E402

# A token from this set anywhere in argv means "the caller wants the CLI".
COMMANDS = {
    "index", "search", "watch", "eval", "doctor", "privacy", "mcp",
    "cheat", "stats", "connect", "serve", "app", "autostart", "seal",
    "shortcut", "install", "ocr",
    # The personal layer and the migration: `run.py pin`, `run.py import kensaku`.
    "pin", "pins", "saved", "alias", "sym", "import",
    # Corpus tools, exports and maintenance: `run.py backup`, `run.py update`, …
    "export", "uninstall", "clip", "backup", "share", "open", "pin-top",
    "tag", "watch-status", "changelog", "register-url", "config", "verify",
    "update", "compact", "big", "stale", "dupes", "dead", "top", "note",
    "model", "vault",
}


def launcher_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="AutoFind: local search over files and screenshots",
        epilog="Subcommands: python run.py <index|search|app|autostart|...> (see `python run.py cheat`)",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="start the server without opening anything")
    parser.add_argument("--browser", action="store_true", help="use the browser instead of the app window")
    parser.add_argument("--db", type=Path, default=None)
    # No literal here: run_app() resolves hotkeys.json:focus when this is unset.
    parser.add_argument("--hotkey", default=None, help="show/hide the window (default: hotkeys.json)")
    parser.add_argument("--no-hotkey", action="store_true")
    parser.add_argument("--no-tray", action="store_true")
    parser.add_argument("--hidden", action="store_true", help="start in the tray (used at login)")
    parser.add_argument("--windowed", action="store_true",
                        help="framed, resizable window instead of borderless")
    parser.add_argument("--wait", action="store_true",
                        help="stay attached to the GUI process (console stays open)")
    parser.add_argument("--debug", action="store_true")
    return parser


def _pid_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5
        )
        # A recycled PID belonging to another process must not look like a
        # resident shell: otherwise run.py exits without opening any window.
        return str(pid) in result.stdout and "autofind-shell" in result.stdout.lower()
    except Exception:
        return False


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

    # Native WebView2 shell: the exact web UI, no Python GUI stack on the
    # hot path. It stays resident (like the kensaku tray app), so a second
    # launch only raises the existing window. It falls back to pywebview when
    # the binary has not been built.
    if not args.browser and not args.no_browser:
        name = "autofind-shell.exe" if os.name == "nt" else "autofind-shell"
        candidates = []
        if getattr(sys, "frozen", False):
            # PyInstaller one-folder: spec-bundled shell lands in _internal,
            # a hand-copied one may sit beside the exe.
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(Path(meipass) / name)
            candidates.append(Path(sys.executable).resolve().parent / name)
        else:
            root = Path(__file__).resolve().parent
            candidates.append(root / "shell-app" / "target" / "release" / name)
        shell = next((c for c in candidates if c.is_file()), None)
        home = getattr(config, "home", None)
        if shell is not None and isinstance(home, (str, Path)):
            home = Path(home)
            pid_file = home / "shell.pid"
            try:
                pid = int(pid_file.read_text().strip())
            except (OSError, ValueError):
                pid = 0
            if pid and _pid_alive(pid):
                (home / "shell-show.flag").write_text("1")
                return 0
            env = os.environ.copy()
            env["PYTHON"] = sys.executable
            env["AUTOFIND_HOME"] = str(home)
            if getattr(sys, "frozen", False):
                # The shell must spawn `AutoFind.exe serve`, not `-m core.cli`.
                env["AUTOFIND_FROZEN"] = "1"
            env["AUTOFIND_PORT"] = str(args.port)
            flags = 0
            if os.name == "nt":
                # Closing the terminal that launched run.py must not take the
                # GUI down with it. A detached child owns its own process group.
                flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            child = subprocess.Popen(
                [str(shell)], cwd=str(Path(__file__).resolve().parent if not
                                      getattr(sys, "frozen", False)
                                      else Path(sys.executable).resolve().parent),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
            return child.wait() if args.wait else 0

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

    app_kwargs: dict = {
        "port": args.port,
        "tray": not args.no_tray,
        "start_hidden": args.hidden,
        "borderless": not args.windowed,
        "debug": args.debug,
    }
    if args.no_hotkey:
        app_kwargs["hotkey"] = None          # explicitly disabled
    elif args.hotkey:
        app_kwargs["hotkey"] = args.hotkey   # explicit override
    # Otherwise omit the key entirely so run_app reads the configured chord.
    return run_app(db_path, config, **app_kwargs)


if __name__ == "__main__":
    raise SystemExit(main())
