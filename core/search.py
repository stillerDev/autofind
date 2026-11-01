"""Hybrid search: BM25 and vector candidates fused with reciprocal rank fusion.

RRF needs no weight training, which is why the plan picks it for week 1: tuning
can wait until there are benchmarks to tune against.

Filters (kind, folder, date) are applied to the candidate pool before fusion,
not to the final list, so a restricted search still returns a full page.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from .store import Store

RRF_K = 60
# Channel weights for the fusion. Equal weights are the textbook default, but
# they assume both channels are equally good. The vector weight is taken from
# the embedder itself (`rrf_weight`): a semantic model gets 1.0, the hashed
# fallback 0.3, because it largely duplicates BM25 and ranks worse. Measured
# with `semfind eval --ablate`; re-measure before trusting it on another corpus.
RRF_WEIGHTS = {"bm25": 1.0, "vector": 0.3}
TOKEN_RE = re.compile(r"\w+", re.UNICODE)
SNIPPET_WINDOW = 180


@dataclass(slots=True)
class Hit:
    chunk_id: int
    file_id: int
    path: str
    kind: str
    ord: int
    score: float
    text: str
    snippet: str
    channels: tuple[str, ...] = field(default_factory=tuple)
    mtime: float = 0.0
    width: int = 0
    height: int = 0

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def name(self) -> str:
        return self.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


@dataclass(slots=True)
class ImageHit:
    image_id: int
    file_id: int
    path: str
    score: float
    width: int
    height: int
    ocr_text: str = ""

    @property
    def name(self) -> str:
        return self.path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


def build_match_query(text: str, max_terms: int = 16) -> str | None:
    """Turn free text into a safe FTS5 query: quoted terms, OR-joined, prefix-matched.

    Russian has no stemmer in SQLite, so a query for «схема» misses «схемы» and
    «схеме». For words long enough to be unambiguous the term is also added as a
    shortened prefix, which is a cheap stand-in for stemming. Short words are
    left alone: «база» -> «ба» would match half the corpus.
    """
    terms: list[str] = []
    for token in TOKEN_RE.findall(text):
        token = token.lower()
        if len(token) < 2:
            continue
        terms.append(token)
        if len(token) >= 6:
            terms.append(token[:-2])
    terms = list(dict.fromkeys(terms))[:max_terms]
    if not terms:
        return None
    return " OR ".join(f'"{term}"*' for term in terms)


def hybrid_search(
    store: Store,
    embedder,
    query: str,
    limit: int = 10,
    candidates: int = 50,
    k: int = RRF_K,
    kinds: Sequence[str] | None = None,
    folder: str | None = None,
    since: float | None = None,
    channels: Sequence[str] = ("bm25", "vector"),
    weights: dict[str, float] | None = None,
) -> list[Hit]:
    filtered = bool(kinds or folder or since)
    pool = candidates * 4 if filtered else candidates
    channel_weights = dict(RRF_WEIGHTS)
    channel_weights["vector"] = float(getattr(embedder, "rrf_weight", RRF_WEIGHTS["vector"]))
    if weights:
        channel_weights.update(weights)

    match = build_match_query(query) if "bm25" in channels else None
    lexical = store.bm25(match, pool) if match else []
    # No lexical match plus a non-semantic fallback embedder means every vector
    # "hit" is noise: hashed features have no notion of meaning, so a query like
    # «кофемашина» matches spinner scripts on hash collisions alone. Returning
    # nothing is the honest answer.
    if "bm25" in channels and not lexical and not getattr(embedder, "semantic", False):
        return []
    semantic = store.knn(embedder.encode_query(query), pool) if "vector" in channels else []

    candidate_ids = {chunk_id for chunk_id, _ in lexical} | {chunk_id for chunk_id, _ in semantic}
    rows = store.chunk_rows(candidate_ids)

    def allowed(chunk_id: int) -> bool:
        row = rows.get(chunk_id)
        if row is None:
            return False
        if kinds and row["kind"] not in kinds:
            return False
        if folder and not str(row["path"]).lower().startswith(folder.lower()):
            return False
        if since is not None and float(row["mtime"]) < since:
            return False
        return True

    fused: dict[int, float] = {}
    channels: dict[int, list[str]] = {}
    for channel, results in (("bm25", lexical), ("vector", semantic)):
        rank = 0
        weight = channel_weights.get(channel, 1.0)
        for chunk_id, _ in results:
            if not allowed(chunk_id):
                continue
            rank += 1
            fused[chunk_id] = fused.get(chunk_id, 0.0) + weight / (k + rank)
            channels.setdefault(chunk_id, []).append(channel)

    ordered = sorted(fused.items(), key=lambda item: -item[1])[:limit]
    hits: list[Hit] = []
    for chunk_id, score in ordered:
        row = rows[chunk_id]
        text = row["text"]
        hit = Hit(
            chunk_id=chunk_id,
            file_id=int(row["file_id"]),
            path=row["path"],
            kind=row["kind"],
            ord=int(row["ord"]),
            score=round(score, 6),
            text=text,
            snippet=make_snippet(text, query),
            channels=tuple(channels.get(chunk_id, [])),
            mtime=float(row["mtime"]),
        )
        if hit.is_image:
            image = store.image_by_file_id(hit.file_id)
            if image is not None:
                hit.width = int(image["width"] or 0)
                hit.height = int(image["height"] or 0)
        hits.append(hit)
    return hits


def similar_images(store: Store, visual, file_id: int, limit: int = 12) -> list[ImageHit]:
    """Find screenshots whose layout and colours resemble the given one."""
    source = store.image_by_file_id(file_id)
    if source is None:
        return []
    ids, matrix = store.image_index(dim=None)
    if not len(ids):
        return []
    position = None
    for index, image_id in enumerate(ids.tolist()):
        if image_id == int(source["id"]):
            position = index
            break
    if position is None:
        return []

    query = matrix[position]
    results = store.knn_images(query, limit + 1)
    hits: list[ImageHit] = []
    for image_id, score in results:
        if image_id == int(source["id"]):
            continue
        row = store.conn.execute(
            "SELECT i.id, i.file_id, i.path, i.width, i.height, i.ocr_text FROM images i WHERE i.id = ?",
            (image_id,),
        ).fetchone()
        if row is None or row["file_id"] is None:
            continue
        hits.append(
            ImageHit(
                image_id=int(row["id"]),
                file_id=int(row["file_id"]),
                path=row["path"],
                score=round(score, 4),
                width=int(row["width"] or 0),
                height=int(row["height"] or 0),
                ocr_text=(row["ocr_text"] or "")[:200],
            )
        )
        if len(hits) >= limit:
            break
    return hits


def visual_text_search(store: Store, visual, query: str, limit: int = 12) -> list[ImageHit]:
    """Text-to-image search. Only CLIP has a text encoder; layout features do not."""
    encoder = getattr(visual, "encode_text", None)
    if encoder is None:
        return []
    vector = encoder(query)
    if vector is None:
        return []
    hits: list[ImageHit] = []
    for image_id, score in store.knn_images(vector, limit):
        row = store.conn.execute(
            "SELECT i.id, i.file_id, i.path, i.width, i.height, i.ocr_text FROM images i WHERE i.id = ?",
            (image_id,),
        ).fetchone()
        if row is None or row["file_id"] is None:
            continue
        hits.append(
            ImageHit(
                image_id=int(row["id"]),
                file_id=int(row["file_id"]),
                path=row["path"],
                score=round(score, 4),
                width=int(row["width"] or 0),
                height=int(row["height"] or 0),
                ocr_text=(row["ocr_text"] or "")[:200],
            )
        )
    return hits


def make_snippet(text: str, query: str) -> str:
    lowered = text.lower()
    position = -1
    for term in TOKEN_RE.findall(query.lower()):
        if len(term) < 3:
            continue
        position = lowered.find(term)
        if position != -1:
            break
    if position == -1:
        return text[:SNIPPET_WINDOW].strip()
    start = max(position - SNIPPET_WINDOW // 3, 0)
    end = min(start + SNIPPET_WINDOW, len(text))
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def rank_paths(hits: Sequence[Hit]) -> list[tuple[str, float]]:
    """Collapse chunk hits into a per-file ranking, keeping the best chunk score."""
    best: dict[str, float] = {}
    for hit in hits:
        best[hit.path] = max(best.get(hit.path, 0.0), hit.score)
    return sorted(best.items(), key=lambda item: -item[1])
