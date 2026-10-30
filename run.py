"""SemFind launcher.

Starts the local UI and opens it in the default browser. First run has an empty
index; the sidebar offers the usual folders (Documents, Downloads, Desktop,
Screenshots) with one button.

    python run.py            # UI on the first free port from 8765
    python run.py --port 9000 --no-browser
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.api import serve  # noqa: E402
from core.config import IndexConfig  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="SemFind local search")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--db", type=Path, default=None)
    args = parser.parse_args()

    config = IndexConfig()
    config.ensure_home()
    db_path = args.db or config.db_path
    serve(db_path, config, port=args.port, open_browser=not args.no_browser)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
