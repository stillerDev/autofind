"""Relevance + latency benchmark on a shared, labelled query set.

Both engines answer the same 30 queries against their own real indexes. The
ground truth is a file whose name contains a token unique to that file among
the 18,417 paths indexed by BOTH engines, so the comparison has identical
targets and identical query strings.

Usage:
    python tools/bench_relevance.py [--queries tools/relevance_queries.json] [--repeat 5]

Requires: AutoFind HTTP on :8765, kensaku HTTP on :8766 with a Bearer token in
%APPDATA%\\kensaku\\http.token. Read-only: no index is modified.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import pathlib
import statistics
import time
import urllib.parse

DEFAULT_QUERIES = pathlib.Path(__file__).resolve().parent / "relevance_queries.json"


def kensaku_token() -> str:
    path = pathlib.Path.home() / "AppData" / "Roaming" / "kensaku" / "http.token"
    return path.read_text(encoding="utf-8").strip()


def request(conn: http.client.HTTPConnection, query: str, engine: str, token: str,
            limit: int = 10) -> tuple[float, list[str]]:
    quoted = urllib.parse.quote(query, safe="")
    if engine == "autofind":
        path = f"/api/search?q={quoted}&limit={limit}"
        headers = {}
    else:
        path = f"/search?q={quoted}&limit={limit}"
        headers = {"Authorization": "Bearer " + token}
    started = time.perf_counter()
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    payload = json.loads(response.read())
    elapsed = (time.perf_counter() - started) * 1000
    items = payload.get("hits") if engine == "autofind" else payload.get("results")
    return elapsed, [str(item.get("path", "")) for item in (items or [])]


def rank_of(paths: list[str], target: str) -> int:
    wanted = target.casefold()
    for index, path in enumerate(paths, 1):
        if path.casefold() == wanted:
            return index
    return 0


def iou(a: list[str], b: list[str], k: int = 5) -> float:
    sa, sb = {p.casefold() for p in a[:k]}, {p.casefold() for p in b[:k]}
    return len(sa & sb) / len(sa | sb) if sa | sb else 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queries", type=pathlib.Path, default=DEFAULT_QUERIES)
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()

    queries = json.loads(args.queries.read_text(encoding="utf-8"))
    token = kensaku_token()
    conns = {
        "autofind": http.client.HTTPConnection("127.0.0.1",
                                       int(os.environ.get("AUTOFIND_TEST_PORT", "8765")),
                                       timeout=30),
        "kensaku": http.client.HTTPConnection("127.0.0.1", 8766, timeout=30),
    }
    rows = []
    for item in queries:
        query, target = item["token"], item["path"]
        measured: dict[str, list[float]] = {"autofind": [], "kensaku": []}
        top: dict[str, list[str]] = {}
        for engine, conn in conns.items():
            request(conn, query, engine, token)  # warm-up, not measured
            for _ in range(args.repeat):
                elapsed, paths = request(conn, query, engine, token)
                measured[engine].append(elapsed)
                top[engine] = paths
        rows.append({
            "query": query,
            "target": target,
            "af_rank": rank_of(top["autofind"], target),
            "ks_rank": rank_of(top["kensaku"], target),
            "af_ms": statistics.median(measured["autofind"]),
            "ks_ms": statistics.median(measured["kensaku"]),
            "af_p95": sorted(measured["autofind"])[min(len(measured["autofind"]) - 1,
                             int(0.95 * len(measured["autofind"])))],
            "ks_p95": sorted(measured["kensaku"])[min(len(measured["kensaku"]) - 1,
                             int(0.95 * len(measured["kensaku"])))],
            "overlap5": iou(top["autofind"], top["kensaku"]),
            "af_top5": top["autofind"][:5],
            "ks_top5": top["kensaku"][:5],
        })
    for conn in conns.values():
        conn.close()

    def metrics(rank_key: str) -> dict[str, float]:
        ranks = [row[rank_key] for row in rows]
        found = [r for r in ranks if r]
        return {
            "recall@1": sum(r == 1 for r in ranks) / len(ranks),
            "recall@5": sum(0 < r <= 5 for r in ranks) / len(ranks),
            "recall@10": sum(0 < r <= 10 for r in ranks) / len(ranks),
            "mrr@10": sum(1 / r for r in found if r <= 10) / len(ranks),
            "mean_rank_when_found": statistics.mean(found) if found else 0.0,
        }

    report = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "queries": len(rows),
        "repeat": args.repeat,
        "ground_truth": "unique filename token; target present in both indexes",
        "autofind": metrics("af_rank"),
        "kensaku": metrics("ks_rank"),
        "median_latency_ms": {
            "autofind": statistics.median(row["af_ms"] for row in rows),
            "kensaku": statistics.median(row["ks_ms"] for row in rows),
        },
        "p95_latency_ms": {
            "autofind": statistics.median(row["af_p95"] for row in rows),
            "kensaku": statistics.median(row["ks_p95"] for row in rows),
        },
        "overlap5_mean": statistics.mean(row["overlap5"] for row in rows),
        "rows": rows,
    }
    out = pathlib.Path(__file__).resolve().parent.parent / "RELEVANCE_RESULT.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"queries={len(rows)} repeat={args.repeat} written to {out}")
    for engine in ("autofind", "kensaku"):
        m = report[engine]
        print(engine, " ".join(f"{k}={v:.3f}" for k, v in m.items()),
              "median_ms", round(report["median_latency_ms"][engine], 2))
    print("overlap@5 mean", round(report["overlap5_mean"], 3))
    for row in rows:
        print(f"{row['query'][:40]:42s} af={row['af_rank'] or '-':>3} "
              f"ks={row['ks_rank'] or '-':>3} ms {row['af_ms']:.2f}/{row['ks_ms']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
