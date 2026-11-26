"""Hard relevance benchmark: two-token queries with an objective ground truth.

For every query the relevant set is the files whose *path* contains both query
tokens, computed from the 18,417 files indexed by both engines. Measures
Recall@5, Precision@5, MRR and latency on identical query strings.

Usage: python tools/bench_relevance_hard.py [--repeat 5]
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

QUERIES = pathlib.Path(__file__).resolve().parent / "relevance_queries_names.json"
OUT = pathlib.Path(__file__).resolve().parent.parent / "RELEVANCE_NAME_RESULT.json"


def token() -> str:
    path = pathlib.Path.home() / "AppData" / "Roaming" / "kensaku" / "http.token"
    return path.read_text(encoding="utf-8").strip()


def ask(conn: http.client.HTTPConnection, query: str, engine: str, bearer: str,
        limit: int = 10) -> tuple[float, list[str]]:
    quoted = urllib.parse.quote(query, safe="")
    if engine == "autofind":
        path, headers = f"/api/search?q={quoted}&limit={limit}", {}
    else:
        path = f"/search?q={quoted}&limit={limit}"
        headers = {"Authorization": "Bearer " + bearer}
    started = time.perf_counter()
    conn.request("GET", path, headers=headers)
    payload = json.loads(conn.getresponse().read())
    elapsed = (time.perf_counter() - started) * 1000
    items = payload.get("hits") if engine == "autofind" else payload.get("results")
    return elapsed, [str(item.get("path", "")) for item in (items or [])]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=5)
    args = parser.parse_args()

    queries = json.loads(QUERIES.read_text(encoding="utf-8"))
    bearer = token()
    conns = {
        "autofind": http.client.HTTPConnection("127.0.0.1",
                                       int(os.environ.get("AUTOFIND_TEST_PORT", "8765")),
                                       timeout=30),
        "kensaku": http.client.HTTPConnection("127.0.0.1", 8766, timeout=30),
    }
    rows = []
    for item in queries:
        query = item["query"]
        relevant = {p.casefold() for p in item["relevant"]}
        samples: dict[str, list[float]] = {"autofind": [], "kensaku": []}
        top: dict[str, list[str]] = {}
        for engine, conn in conns.items():
            ask(conn, query, engine, bearer)
            for _ in range(args.repeat):
                elapsed, paths = ask(conn, query, engine, bearer)
                samples[engine].append(elapsed)
                top[engine] = paths
        row = {"query": query, "relevant_n": len(relevant), "target": item["target"]}
        for engine in conns:
            ranked = [p.casefold() for p in top[engine]]
            first = next((i for i, p in enumerate(ranked, 1) if p in relevant), 0)
            found5 = len([p for p in ranked[:5] if p in relevant])
            row[f"{engine}_ms"] = statistics.median(samples[engine])
            row[f"{engine}_p95"] = sorted(samples[engine])[min(len(samples[engine]) - 1,
                                              int(0.95 * len(samples[engine])))]
            row[f"{engine}_r5"] = found5 / len(relevant)
            row[f"{engine}_p5"] = found5 / 5
            row[f"{engine}_mrr"] = (1 / first) if first else 0.0
            row[f"{engine}_first"] = first
            row[f"{engine}_top5"] = top[engine][:5]
        sa = {p.casefold() for p in row["autofind_top5"]}
        sb = {p.casefold() for p in row["kensaku_top5"]}
        row["overlap5"] = len(sa & sb) / len(sa | sb) if sa | sb else 0.0
        rows.append(row)
    for conn in conns.values():
        conn.close()

    def agg(prefix: str) -> dict[str, float]:
        return {
            "recall@5": statistics.mean(r[f"{prefix}_r5"] for r in rows),
            "precision@5": statistics.mean(r[f"{prefix}_p5"] for r in rows),
            "mrr": statistics.mean(r[f"{prefix}_mrr"] for r in rows),
            "success@1": sum(r[f"{prefix}_first"] == 1 for r in rows) / len(rows),
            "success@5": sum(bool(r[f"{prefix}_first"]) and r[f"{prefix}_first"] <= 5 for r in rows) / len(rows),
            "median_ms": statistics.median(r[f"{prefix}_ms"] for r in rows),
            "median_p95_ms": statistics.median(r[f"{prefix}_p95"] for r in rows),
        }

    report = {
        "date": time.strftime("%Y-%m-%d %H:%M:%S"),
        "queries": len(rows),
        "repeat": args.repeat,
        "ground_truth": "paths containing both query tokens, shared by both indexes",
        "autofind": agg("autofind"),
        "kensaku": agg("kensaku"),
        "overlap5_mean": statistics.mean(r["overlap5"] for r in rows),
        "rows": rows,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written", OUT)
    for engine in ("autofind", "kensaku"):
        print(engine, " ".join(f"{k}={v:.3f}" for k, v in report[engine].items()))
    print("overlap@5", round(report["overlap5_mean"], 3))
    for r in rows:
        print(f"{r['query'][:34]:36s} R={r['relevant_n']} af={r['autofind_first'] or '-':>2} "
              f"ks={r['kensaku_first'] or '-':>2} ms={r['autofind_ms']:.2f}/{r['kensaku_ms']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
