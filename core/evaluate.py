"""Retrieval quality harness.

The plan pins the acceptance criterion to Recall@10 >= 0.7 on 20 queries. A
number is only worth something if it can be reproduced and if the queries were
not written after seeing the results, so the labels live in a JSON file next to
the corpus and every report states which channel set produced it.

Reported per run:
  recall@k   share of queries with at least one expected file in the top k
  mrr        mean reciprocal rank of the first expected file
  latency    p50 / p95 of the query itself, excluding index loading
  ablation   the same numbers for bm25 only, vector only and the fusion
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path

from .search import hybrid_search, rank_paths
from .store import Store


@dataclass(slots=True)
class QueryScore:
    query: str
    expected: list[str]
    rank: int | None
    top: list[str]
    ms: float
    note: str = ""

    @property
    def hit(self) -> bool:
        return self.rank is not None

    def as_dict(self, limit: int) -> dict:
        return {
            "query": self.query,
            "expected": self.expected,
            "rank": self.rank,
            "hit": self.rank is not None and self.rank <= limit,
            "top": self.top[:3],
            "ms": round(self.ms, 1),
            "note": self.note,
        }


@dataclass(slots=True)
class EvalReport:
    label: str
    limit: int
    channels: tuple[str, ...]
    scores: list[QueryScore] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.scores)

    @property
    def recall(self) -> float:
        if not self.scores:
            return 0.0
        return sum(1 for score in self.scores if score.rank and score.rank <= self.limit) / self.n

    @property
    def mrr(self) -> float:
        if not self.scores:
            return 0.0
        return sum(1.0 / score.rank for score in self.scores if score.rank) / self.n

    @property
    def latency(self) -> dict:
        values = sorted(score.ms for score in self.scores)
        if not values:
            return {"p50": 0.0, "p95": 0.0}
        p50 = statistics.median(values)
        p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
        return {"p50": round(p50, 1), "p95": round(p95, 1)}

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "limit": self.limit,
            "channels": list(self.channels),
            "queries": self.n,
            "recall": round(self.recall, 3),
            "mrr": round(self.mrr, 3),
            "latency": self.latency,
            "misses": [
                score.as_dict(self.limit) for score in self.scores if not score.rank or score.rank > self.limit
            ],
            "details": [score.as_dict(self.limit) for score in self.scores],
        }


def load_labels(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    queries = data.get("queries") or []
    if not isinstance(queries, list) or not queries:
        raise ValueError(f"{path}: no queries")
    for item in queries:
        if not item.get("q") or not item.get("expected"):
            raise ValueError(f"{path}: every query needs 'q' and 'expected'")
    return data


def matches(path: str, expected: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    needle = expected.replace("\\", "/").lower()
    if needle in lowered:
        return True
    return lowered.rsplit("/", 1)[-1] == needle


def evaluate(
    store: Store,
    embedder,
    labels: dict,
    limit: int = 10,
    candidates: int = 200,
    channels: tuple[str, ...] = ("bm25", "vector"),
    kinds=None,
    weights: dict[str, float] | None = None,
) -> EvalReport:
    report = EvalReport(
        label=str(labels.get("label") or labels.get("corpus") or "unlabelled"),
        limit=limit,
        channels=channels,
    )
    for item in labels["queries"]:
        query = str(item["q"])
        expected = [str(value) for value in item["expected"]]
        started = time.perf_counter()
        hits = hybrid_search(
            store,
            embedder,
            query,
            limit=candidates,
            candidates=candidates,
            kinds=kinds,
            channels=channels,
            weights=weights,
        )
        elapsed = (time.perf_counter() - started) * 1000
        ranked = [path for path, _ in rank_paths(hits)]

        rank = None
        for position, path in enumerate(ranked, start=1):
            if any(matches(path, needle) for needle in expected):
                rank = position
                break

        note = ""
        if rank is None and ranked:
            note = "not in candidate pool"
        elif rank is None:
            note = "no results at all"
        report.scores.append(
            QueryScore(
                query=query,
                expected=expected,
                rank=rank,
                top=ranked[:limit],
                ms=elapsed,
                note=note,
            )
        )
    return report


def compare_channels(
    store: Store, embedder, labels: dict, limit: int = 10, candidates: int = 200
) -> dict[str, EvalReport]:
    """Same labels through each channel set: the ablation the plan asks for."""
    variants = {
        "bm25": ("bm25",),
        "vector": ("vector",),
        "fused": ("bm25", "vector"),
    }
    return {
        name: evaluate(store, embedder, labels, limit=limit, candidates=candidates, channels=channels)
        for name, channels in variants.items()
    }


def format_report(report: EvalReport, verbose: bool = False) -> str:
    lines = [
        f"labels      {report.label}",
        f"queries     {report.n}",
        f"channels    {'+'.join(report.channels)}",
        f"recall@{report.limit}   {report.recall:.3f}",
        f"mrr         {report.mrr:.3f}",
        f"latency     p50 {report.latency['p50']:.0f} ms · p95 {report.latency['p95']:.0f} ms",
    ]
    if verbose:
        lines.append("")
        for position, score in enumerate(report.scores, start=1):
            mark = "ok " if score.rank and score.rank <= report.limit else "MISS"
            where = f"rank {score.rank}" if score.rank else "—"
            lines.append(f"{position:2}. {mark} {where:>8}  {score.query}")
            if not score.rank or score.rank > report.limit:
                lines.append(f"      expected {', '.join(score.expected)}")
                lines.append(f"      got      {', '.join(name for name in score.top[:3]) or 'nothing'}")
    return "\n".join(lines)
