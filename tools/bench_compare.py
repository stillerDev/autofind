"""Reproducible A/B benchmark: AutoFind vs kensaku v1.0.19.

Method (same as the reference run):
  * real indexes on this machine (not synthetic), different corpora;
  * one warm-up query per axis, then 15 measured requests through a keep-alive
    http.client connection; median + p95 in ms;
  * cold CLI = median wall time of 3 fresh processes;
  * RSS of the server processes after warm-up (psutil);
  * active index files only;
  * top-5 path intersection-over-union between the two result lists.

Usage:  python tools/bench_compare.py [--repeat N]
Writes a JSON result into the system temp directory and prints a markdown
summary. No files belonging to either index are modified.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import pathlib
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse

import numpy as np
import psutil

AF_DIR = pathlib.Path(__file__).resolve().parent.parent
AF_HOME = pathlib.Path(os.environ.get("AUTOFIND_TEST_HOME", str(pathlib.Path.home() / ".autofind")))
KS_HOME = pathlib.Path(os.environ.get("APPDATA", "")) / "kensaku"
KS_EXE = pathlib.Path(tempfile.gettempdir()) / "opencode" / "afbench" / "kensaku-cli-windows-x86_64.exe"
# Overridable so a rebuilt server on a different port can be measured without
# stopping the operator's long-running instance.
AF_PORT = int(os.environ.get("AUTOFIND_TEST_PORT", "8765"))
KS_PORT = 8766

QUERIES = [
    "авторизация", "бюджет", "Проект", "invoice", "config", "README", "auth",
    "def search", "static-e5", "numpy", "kensaku", "пароль", "скриншот",
    "report 2026", "test", "main", "index", "watcher", "OCR", "договор",
    "function", "import sqlite3", "class User", "TODO", "JSON API", "python",
    "Windows", "2026", "сметы на март", "中文数据", "ext:py", "ext:md",
    "path:Downloads", "since:iso", "sort:name",
]
LIMIT = 12
REPEAT = 15


def pp95(values: list[float]) -> float:
    return float(np.percentile(np.asarray(values, dtype=float), 95))


class Engine:
    def __init__(self, name: str, host: str, port: int, path: str, headers: dict[str, str], key: str):
        self.name = name
        self.host = host
        self.port = port
        self.path = path
        self.headers = headers
        self.key = key
        self.conn: http.client.HTTPConnection | None = None

    def connect(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except OSError:
                pass
        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=30)

    def get(self, query: str, timed: bool = True) -> tuple[float, dict]:
        url = self.path + urllib.parse.quote(query, safe="") + f"&limit={LIMIT}"
        for attempt in range(2):
            if self.conn is None:
                self.connect()
            started = time.perf_counter()
            try:
                assert self.conn is not None
                self.conn.request("GET", url, headers=self.headers)
                response = self.conn.getresponse()
                body = response.read()
                if response.status != 200:
                    raise RuntimeError(f"{self.name}: HTTP {response.status}: {body[:200]!r}")
                elapsed = (time.perf_counter() - started) * 1000.0
                return (elapsed if timed else 0.0), json.loads(body)
            except (OSError, http.client.HTTPException):
                self.connect()  # kensaku may close the socket after one response
                if attempt:
                    raise
        raise RuntimeError("unreachable")

    def top_paths(self, payload: dict) -> list[str]:
        items = payload.get(self.key) or []
        return [os.path.normcase(str(item["path"])) for item in items[:5]]

    def ping(self) -> dict:
        self.connect()
        _, payload = self.get("ping", timed=False)
        return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=REPEAT)
    args = parser.parse_args()

    token = (KS_HOME / "http.token").read_text(encoding="utf-8").strip()
    engines = [
        Engine("autofind", "127.0.0.1", AF_PORT, "/api/search?q=", {}, "hits"),
        Engine("kensaku", "127.0.0.1", KS_PORT, "/search?q=",
               {"Authorization": "Bearer " + token}, "results"),
    ]
    for engine in engines:
        engine.ping()

    report: dict = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "machine": os.environ.get("COMPUTERNAME", "unknown"),
        "autofind_index_files": None,
        "kensaku_index_files": None,
        "axes": [],
        "cold_cli_ms": {},
        "rss_mib": {},
        "disk_bytes": {},
        "indexing": {},
        "overlap_mean": None,
    }

    # First query: before any warm-up, after both servers are up.
    first: dict[str, float] = {}
    for engine in engines:
        first[engine.name], _ = engine.get("test")
    report["first_query_ms"] = first

    # Warm-up: one request per axis, then `repeat` measured requests.
    for query in QUERIES:
        for engine in engines:
            engine.get(query, timed=False)
    rows: list[dict] = []
    overlaps: list[float] = []
    for query in QUERIES:
        samples: dict[str, list[float]] = {}
        tops: dict[str, set[str]] = {}
        for engine in engines:
            values: list[float] = []
            for _ in range(args.repeat):
                elapsed, payload = engine.get(query)
                values.append(elapsed)
                tops[engine.name] = set(engine.top_paths(payload))
            samples[engine.name] = values
        a, b = tops["autofind"], tops["kensaku"]
        overlap = len(a & b) / len(a | b) if a or b else 0.0
        overlaps.append(overlap)
        rows.append({
            "query": query,
            "af_median": statistics.median(samples["autofind"]),
            "af_p95": pp95(samples["autofind"]),
            "ks_median": statistics.median(samples["kensaku"]),
            "ks_p95": pp95(samples["kensaku"]),
            "overlap": overlap,
            "af_total": None, "ks_total": None,
        })
    report["axes"] = rows
    report["overlap_mean"] = statistics.fmean(overlaps)

    # RSS of the two server processes after warm-up.
    for engine in engines:
        pid = None
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr.port == engine.port:
                pid = conn.pid
                break
        if pid:
            report["rss_mib"][engine.name] = round(psutil.Process(pid).memory_info().rss / (1 << 20), 1)

    # Active index bytes only (no backups, no thumbnails/models).
    af_files = ["index.db", "index.db-wal", "index.db.bm25seal", "index.db.nameidx"]
    af_files += [p.name for p in AF_HOME.glob("index.db.nameidx.*.bin")]
    report["disk_bytes"]["autofind"] = {
        name: (AF_HOME / name).stat().st_size for name in af_files if (AF_HOME / name).is_file()
    }
    ks_index = [p for p in (KS_HOME / "index").iterdir() if p.is_file()]
    ks_db = [p for p in KS_HOME.glob("metadata.db*") if p.is_file()]
    report["disk_bytes"]["kensaku"] = {
        p.name: p.stat().st_size for p in ks_index + ks_db
    }

    # Cold CLI: median of three fresh processes.
    # The real CLI invocation delegates to the already-running server (the
    # feature exists exactly for this); `--local` is the fallback and is kept
    # as a second data point so the number cannot be mistaken for the default.
    cmds = {
        "autofind": [sys.executable, "-m", "core.cli", "search", "test", "--json", "-n", "5"],
        "autofind_local": [sys.executable, "-m", "core.cli", "search", "test",
                           "--json", "--local", "-n", "5"],
        "kensaku": [str(KS_EXE), "search", "test", "--json", "--limit", "5"],
    }
    for name, cmd in cmds.items():
        times = []
        for _ in range(3):
            start = time.perf_counter()
            completed = subprocess.run(cmd, cwd=str(AF_DIR), capture_output=True, timeout=180)
            times.append((time.perf_counter() - start) * 1000.0)
            if completed.returncode != 0:
                report.setdefault("cli_errors", []).append(
                    f"{name}: {completed.returncode}: {completed.stderr[-300:]!r}"
                )
        report["cold_cli_ms"][name] = round(statistics.median(times), 1)

    out = pathlib.Path(tempfile.gettempdir()) / "opencode" / "afbench" / "bench_result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"# A/B benchmark {report['date']} on {report['machine']}")
    print(f"axes: {len(rows)}, repeat: {args.repeat}, overlap mean: {report['overlap_mean']:.3f}")
    print(f"first query ms: {report['first_query_ms']}")
    print(f"cold CLI ms: {report['cold_cli_ms']}")
    print(f"RSS MiB: {report['rss_mib']}")
    for name, files in report["disk_bytes"].items():
        print(f"disk {name}: {sum(files.values())} bytes ({len(files)} files)")
    print("| query | ks med | ks p95 | af med | af p95 | overlap |")
    print("|---|---:|---:|---:|---:|---:|")
    for row in rows:
        print(f"| {row['query']} | {row['ks_median']:.2f} | {row['ks_p95']:.2f} | "
              f"{row['af_median']:.2f} | {row['af_p95']:.2f} | {row['overlap']:.2f} |")
    print(f"result: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
