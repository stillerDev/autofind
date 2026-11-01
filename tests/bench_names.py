"""Synthetic scale test for the name index: 8k -> 100k -> 1M names.

Run: python -m tests.bench_names
"""

from __future__ import annotations

import random
import time

import numpy as np

from core.nameindex import NameIndex
from core.store import char_mask

WORDS = [
    "report", "invoice", "screenshot", "design", "backup", "config", "readme",
    "notes", "budget", "photo", "resume", "draft", "final", "meeting", "log",
    "отчет", "схема", "договор", "смета", "письмо", "заметки", "проект",
]
EXTS = [".pdf", ".png", ".docx", ".py", ".ts", ".md", ".xlsx", ".jpg", ".txt", ".rs"]


def make_rows(count: int, seed: int = 7) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    for index in range(count):
        name = f"{rng.choice(WORDS)}_{index:07d}{rng.choice(EXTS)}"
        rows.append({
            "id": index + 1,
            "path": f"C:\\Users\\someone\\Documents\\folder{index % 97}\\{name}",
            "name": name,
            "char_mask": char_mask(name),
            "kind": "doc",
            "size": rng.randint(1000, 5_000_000),
            "mtime": 1_700_000_000.0 + rng.randint(0, 30_000_000),
        })
    return rows


def bench(count: int, queries: list[str], repeat: int = 5) -> None:
    rows = make_rows(count)
    started = time.perf_counter()
    index = NameIndex.from_rows(rows)
    build = (time.perf_counter() - started) * 1000

    print(f"\n=== {count:,} names — build {build:.0f} ms, "
          f"{(index.ids.nbytes + index.masks.nbytes + index.sizes.nbytes + index.mtimes.nbytes) / 1e6:.1f} MB arrays")

    for query in queries:
        best = 1e9
        for _ in range(repeat):
            started = time.perf_counter()
            hits = index.search(query, 10)
            best = min(best, (time.perf_counter() - started) * 1000)
        print(f"  {query:<10} {best:7.2f} ms   {len(hits):2d} hits")


if __name__ == "__main__":
    queries = ["rdme", "screen", "invoice", "test", "отчет", "s"]
    for count in (8_000, 100_000, 1_000_000):
        bench(count, queries, repeat=3)
