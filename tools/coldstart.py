"""Measure cold start: process spawn -> window up -> index warm -> first search.

    python tools/coldstart.py            # three runs, median
    python tools/coldstart.py --runs 5
    python tools/coldstart.py --windowed

The interesting number is not "how fast does the window paint" but "how long
until the first query is fast", because that is what the user waits for. Three
timestamps are recorded separately:

* **http** — the port answers. Python is up, the server thread is running.
* **window** — a top-level window titled AutoFind exists and is visible.
* **ready** — `/api/ready` reports `ready: true`, which the server sets after the
  vector pack has been faulted in. This is the end of the loading screen.

`/api/ready` also reports `uptime_ms`, measured inside the process from the
moment `core.api` was imported. The gap between `ready` and `uptime_ms` is the
interpreter and import cost — worth knowing, because it is the part no amount of
optimising our own code can remove.

A true cold start also needs the OS page cache dropped, which needs
administrator rights; what this measures is a start with the index on disk and a
warm-ish file cache. The `--runs` median is the honest number to quote.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TITLE = "AutoFind"


def free_port(start: int = 8910) -> int:
    import socket

    for candidate in range(start, start + 60):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError("no free port")


def get(url: str, timeout: float = 0.4):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def window_visible(title: str) -> bool:
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)
    def callback(hwnd, _):
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length:
            buffer = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value.strip() == title:
                found.append(bool(ctypes.windll.user32.IsWindowVisible(hwnd)))
        return True

    ctypes.windll.user32.EnumWindows(callback, 0)
    return any(found)


def kill_tree(process: subprocess.Popen) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        capture_output=True,
        check=False,
    )
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def run_once(port: int, windowed: bool, timeout: float) -> dict:
    python = Path(sys.executable)
    launcher = python.with_name("pythonw.exe")
    if not launcher.exists():
        launcher = python

    args = [str(launcher), str(ROOT / "run.py"), "--port", str(port)]
    if windowed:
        args.append("--windowed")

    started = time.perf_counter()
    process = subprocess.Popen(args, cwd=str(ROOT))
    marks: dict[str, float | None] = {"http": None, "window": None, "ready": None}
    state: dict = {}
    try:
        while time.perf_counter() - started < timeout:
            if marks["window"] is None and window_visible(TITLE):
                marks["window"] = time.perf_counter() - started
            try:
                state = get(f"http://127.0.0.1:{port}/api/ready", timeout=0.3)
                if marks["http"] is None:
                    marks["http"] = time.perf_counter() - started
                if state.get("ready"):
                    marks["ready"] = time.perf_counter() - started
                    break
            except (urllib.error.URLError, OSError, ValueError):
                # Not listening yet, or not answering yet: that is the measurement.
                pass
            if process.poll() is not None:
                break
            time.sleep(0.005)

        # The server answers before the window paints — they are on different
        # threads — so keep watching briefly for the window instead of killing
        # the process the moment the index is warm.
        if marks["ready"] is not None and marks["window"] is None:
            deadline = time.perf_counter() + 6.0
            while time.perf_counter() < deadline:
                if window_visible(TITLE):
                    marks["window"] = time.perf_counter() - started
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.02)

        search_ms = None
        try:
            began = time.perf_counter()
            get(f"http://127.0.0.1:{port}/api/search?q=test&limit=5", timeout=10)
            search_ms = (time.perf_counter() - began) * 1000.0
        except Exception:
            pass
    finally:
        kill_tree(process)

    return {
        **marks,
        "uptime_ms": state.get("uptime_ms"),
        "warm_ms": state.get("warm_ms"),
        "search_ms": search_ms,
        "stats": state.get("stats"),
    }


def report(runs: list[dict]) -> None:
    columns = [
        ("http", "порт отвечает"),
        ("window", "окно видно"),
        ("ready", "индекс готов"),
    ]
    print(f"{'':<16}" + "".join(f"run {i + 1:<6}" for i in range(len(runs))) + "  медиана")
    for key, label in columns:
        values = [run[key] for run in runs if run.get(key) is not None]
        if not values:
            print(f"{label:<16}—")
            continue
        cells = "".join(
            f"{(run[key] if run.get(key) is not None else float('nan')):<10.3f}" for run in runs
        )
        print(f"{label:<16}{cells}  {statistics.median(values):.3f} с")

    warm = [run["warm_ms"] for run in runs if run.get("warm_ms") is not None]
    if warm:
        print(f"{'грелка векторов':<16}" + " " * (len(runs) * 10) + f"  {statistics.median(warm):.0f} мс")
    searches = [run["search_ms"] for run in runs if run.get("search_ms") is not None]
    if searches:
        print(f"{'первый поиск':<16}" + " " * (len(runs) * 10) + f"  {statistics.median(searches):.0f} мс")
    ready = [run["ready"] for run in runs if run.get("ready") is not None]
    if ready:
        print()
        print(f"cold start: {statistics.median(ready):.2f} с "
              f"(min {min(ready):.2f}, max {max(ready):.2f}, {len(ready)}/{len(runs)} runs)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="measure AutoFind cold start")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--windowed", action="store_true", help="framed window instead of borderless")
    parser.add_argument("--timeout", type=float, default=40.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if not (ROOT / "core" / "app.py").exists():
        print("run from the project root", file=sys.stderr)
        return 2

    runs: list[dict] = []
    for index in range(args.runs):
        port = free_port(8910 + index * 10)
        print(f"run {index + 1}/{args.runs} on port {port} ...", file=sys.stderr, flush=True)
        runs.append(run_once(port, args.windowed, args.timeout))
        time.sleep(0.8)  # let the port and the window class settle

    if args.json:
        print(json.dumps(runs, ensure_ascii=False, indent=2))
    else:
        report(runs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
