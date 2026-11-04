"""Hybrid search: fuzzy names, BM25 and vectors fused with reciprocal rank fusion.

Three channels, one ranking:

* **fuzzy** — subsequence match over the file name ("rdme" finds README.md),
  prefiltered by a 64-bit character bloom so most of the corpus is rejected
  before the matcher runs.
* **bm25** — SQLite FTS5 over chunk text.
* **vector** — cosine over the chunk embedding matrix.

RRF needs no weight training, which is why the plan picks it for week 1. The
fusion score is normalised to `[0, 1]` before the bonuses from kensaku are
applied — exact/prefix name hits, smooth recency, frecency and a bounded depth
penalty — so an absolute term can never outrank a genuinely better match.

Results are per **file**, not per chunk: the same document matching three times
is one row with the best snippet. Filters are applied to the candidate pool
before fusion, so a restricted search still fills a page.
"""

from __future__ import annotations

import difflib
import math
import re
import time
from dataclasses import dataclass, field
from typing import Sequence

from . import nameindex
from . import cjk
from .cjk import cjk_query_phrase
from .group import group_hits
from .nameindex import subsequence_score
from .personal import expand_aliases
from .query import Hint, ParsedQuery, understand
from .store import Store, basename

RRF_K = 60
# Channel weights for the fusion. Equal weights are the textbook default, but
# they assume both channels are equally good. The vector weight is taken from
# the embedder itself (`rrf_weight`): a semantic model gets 1.0, the hashed
# fallback 0.3, because it largely duplicates BM25 and ranks worse. Measured
# with `semfind eval --ablate`; re-measure before trusting it on another corpus.
# `cjk` is a real lexical index (of bigrams), so it earns an equal share; `tri`
# is the deliberately fuzzy typo tier — kensaku damps its folded tier to ~0.55
# of the lexical weight for the same reason.
RRF_WEIGHTS = {"bm25": 1.0, "vector": 0.3, "fuzzy": 0.7, "cjk": 1.0, "tri": 0.55}
TOKEN_RE = re.compile(r"\w+", re.UNICODE)
CJK_RE = cjk.CJK_RE
SNIPPET_WINDOW = 180

# Bonus weights, lifted from kensaku's `finalize_score`. They are additive on a
# normalised base, so the sum has to stay well under 1.0 or a strong bonus on a
# weak match would beat an exact hit.
NAME_SUBSTRING_BONUS = 0.20
NAME_PREFIX_BONUS = 0.15
RECENCY_WEIGHT = 0.10
RECENCY_TAU_DAYS = 7.0
FRECENCY_WEIGHT = 0.08
FRECENCY_TAU_DAYS = 14.0
# A pinned file is a decision the user made once and should not have to make
# again. Bigger than recency, smaller than a name match: a pin says "I care about
# this", not "this is what I typed".
PIN_BONUS = 0.12
DEPTH_PENALTY = 0.10
DEPTH_SATURATION = 20

SORTS = ("best", "newest", "oldest", "largest", "name")


@dataclass(slots=True)
class Hit:
    """One file. `snippet` is the best chunk of that file, not a whole one."""

    file_id: int
    path: str
    kind: str
    score: float
    snippet: str
    name: str
    ord: int
    mtime: float
    size: int
    channels: tuple[str, ...] = field(default_factory=tuple)
    width: int = 0
    height: int = 0
    chunks: int = 1
    # 64-bit dHash, used to collapse screenshot bursts.
    phash: int = 0
    # Set by the grouping pass: how many rows this one stands for, and which.
    group_size: int = 1
    group_reason: str = ""
    group_members: tuple[int, ...] = ()
    # The personal layer: a pinned file wears a star in the UI, and a `sym:`
    # hit carries the definition it matched so the row can say which one.
    pinned: bool = False
    symbol: str = ""

    @property
    def is_image(self) -> bool:
        return self.kind == "image"


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


@dataclass(slots=True)
class SearchPage:
    """Everything one query returns: rows, counts, chips and the parsed form."""

    hits: list[Hit]
    total: int
    facets: dict[str, int]
    chips: list[dict]
    hints: list[dict]
    query: str
    mode: str
    sort: str
    offset: int
    limit: int
    approx: bool = False
    groups: int = 0
    collapsed: int = 0

    @property
    def next_offset(self) -> int | None:
        if self.offset + len(self.hits) >= self.total:
            return None
        return self.offset + len(self.hits)


def build_match_query(text: str, max_terms: int = 16) -> str | None:
    """Turn free text into a safe FTS5 query: quoted terms, OR-joined, prefix-matched.

    Russian has no stemmer in SQLite, so a query for «схема» misses «схемы» and
    «схеме». For words long enough to be unambiguous the term is also added as a
    shortened prefix, which is a cheap stand-in for stemming. Short words are
    left alone: «база» -> «ба» would match half the corpus.

    CJK runs become phrases of the same bigrams the index stored
    (see core/cjk.py): `数据库` arrives as `"数据 据库"`, which matches the
    chunks_cjk table and enforces the original character order.
    """
    terms: list[str] = []
    for token in TOKEN_RE.findall(text):
        token = token.lower()
        # Split into alternating CJK / non-CJK pieces, keeping both: a mixed
        # token like `CUDA数据` is a normal term plus a CJK phrase.
        for segment in re.split(f"({CJK_RE.pattern})", token):
            if not segment:
                continue
            if CJK_RE.fullmatch(segment):
                if len(segment) == 1:
                    terms.append(f'"{segment}"')  # an exact char, a star is noise
                    continue
                phrase = cjk_query_phrase(segment)
                if phrase:
                    terms.append(f'"{phrase}"')
                continue
            if len(segment) < 2:
                continue
            terms.append(segment)
            if len(segment) >= 6:
                terms.append(segment[:-2])
    terms = list(dict.fromkeys(terms))[:max_terms]
    if not terms:
        return None
    # CJK phrases carry their own quotes; plain terms get quoting plus the
    # prefix star.
    return " OR ".join(
        term if term.startswith('"') else f'"{term}"*' for term in terms
    )


def build_tri_query(text: str, max_terms: int = 8) -> str | None:
    """The typo tier's query: every long term broken into OR-ed 3-grams.

    A mis-typed `invoive` still shares inv, nvo, voi and ive with `invoice`,
    and bm25 inside the trigram table ranks the documents that share more
    grams higher. Far too permissive to be a primary channel, which is why it
    only runs when the exact tiers came back thin — and only at weight 0.55.
    """
    grams: list[str] = []
    for token in TOKEN_RE.findall(text):
        token = token.lower()
        if CJK_RE.search(token) or len(token) < 4:
            continue
        for start in range(len(token) - 2):
            grams.append(token[start : start + 3])
    grams = list(dict.fromkeys(grams))[: 8]
    if not grams:
        return None
    return " OR ".join(f'"{gram}"' for gram in grams)


def scoped_file_ids(store: Store, prefixes: list[str]) -> set[int]:
    """Every file id under any `in:` prefix, from the in-memory path index.

    Vectorised over the lowered path blob — one byte comparison per prefix
    byte — instead of the per-path Python `startswith` loop it replaced.
    Buys fusion slots that matches() would otherwise waste on out-of-scope
    candidates.
    """
    index = nameindex.load(store)
    wanted = [prefix.lower().rstrip(r"\/") for prefix in prefixes]
    positions = index.scope_positions(wanted)
    return {int(fid) for fid in index.ids[positions].tolist()}


def _verify_tri(
    files: Sequence[int], best_chunk_by_file: dict, query_text: str
) -> list[int]:
    """Trigram candidates verified against the raw chunk text.

    The OR over 3-grams reaches for anything sharing three letters with the
    query; before those candidates earn rank positions their chunk has to show
    real evidence: the term itself, or at least two of that term's grams as
    substrings. The folded tier in kensaku verifies the same way.
    """
    terms = [
        token.lower()
        for token in TOKEN_RE.findall(query_text)
        if len(token) >= 4 and not CJK_RE.search(token)
    ]
    grams = {term: {term[i : i + 3] for i in range(len(term) - 2)} for term in terms}
    if not grams:
        return list(files)
    kept: list[int] = []
    for fid in files:
        row = best_chunk_by_file.get(fid)
        text = row["text"].lower() if row is not None else ""
        for term, gram_set in grams.items():
            if term in text or sum(1 for gram in gram_set if gram in text) >= 2:
                kept.append(fid)
                break
    return kept


def _suggest_term(query_text: str, best_chunk_by_file: dict) -> str | None:
    """A corrected spelling from the chunks the typo tier reached.

    The candidates are rank-ordered, so the first close spelling of a query
    term wins — the same document the user is probably looking for.
    """
    terms = [
        token.lower()
        for token in TOKEN_RE.findall(query_text)
        if len(token) >= 4 and not CJK_RE.search(token)
    ]
    if not terms:
        return None
    best: tuple[float, str] | None = None
    examined = 0
    for row in best_chunk_by_file.values():
        text = row["text"].lower()
        for word in set(TOKEN_RE.findall(text)):
            if len(word) < 4 or CJK_RE.search(word):
                continue
            for term in terms:
                if word == term:
                    continue
                ratio = difflib.SequenceMatcher(None, term, word).ratio()
                if ratio >= 0.78 and (best is None or ratio > best[0]):
                    best = (ratio, word)
        examined += 1
        if examined >= 30:
            break
    return best[1] if best else None


# --------------------------------------------------------------------- fuzzy

def fuzzy_files(store: Store, text: str, limit: int) -> list[tuple[int, float]]:
    """Rank files by name similarity, newest first inside equal scores.

    Delegates to the process-wide in-memory name index: the bloom prefilter is
    one vectorised AND over the whole corpus, so this no longer issues a query
    per keystroke and no longer scales with the number of rows in SQLite.
    """
    index = nameindex.load(store)
    return index.search(text, limit)


# ------------------------------------------------------------------- ranking

def finalize_score(
    base: float,
    name: str,
    path: str,
    mtime: float,
    now: float,
    usage: tuple[int, float] = (0, 0.0),
    text: str = "",
    pinned: bool = False,
) -> float:
    """RRF base in [0, 1] plus the bonuses that make a launcher feel right."""
    score = base
    lowered = name.lower()
    needle = text.strip().lower()
    if needle:
        if needle in lowered:
            score += NAME_SUBSTRING_BONUS
        if lowered.startswith(needle):
            score += NAME_PREFIX_BONUS

    age_days = max(now - mtime, 0.0) / 86400.0
    score += RECENCY_WEIGHT * math.exp(-age_days / RECENCY_TAU_DAYS)

    if pinned:
        score += PIN_BONUS

    opens, last_open = usage
    if opens > 0:
        last_days = max(now - last_open, 0.0) / 86400.0
        score += FRECENCY_WEIGHT * math.log1p(opens) * math.exp(-last_days / FRECENCY_TAU_DAYS)

    depth = path.count("\\") + path.count("/")
    score -= DEPTH_PENALTY * min(depth / DEPTH_SATURATION, 1.0)
    return max(score, 0.0)


def _order_key(sort: str):
    if sort == "newest":
        return lambda hit: (-hit.mtime, hit.path)
    if sort == "oldest":
        return lambda hit: (hit.mtime, hit.path)
    if sort == "largest":
        return lambda hit: (-hit.size, hit.path)
    if sort == "name":
        return lambda hit: (hit.name.lower(), hit.path)
    return lambda hit: (-hit.score, hit.path)


def _resolve_sort(requested: str | None, parsed: ParsedQuery, listing: bool) -> str:
    for candidate in (requested, parsed.sort):
        if candidate in SORTS:
            return candidate
    return "newest" if listing else "best"


# ------------------------------------------------------------------- listing

def _listing(
    store: Store,
    parsed: ParsedQuery,
    sort: str,
    since: float | None = None,
) -> tuple[list[Hit], dict[str, int]]:
    """Filter-only queries: `type:image`, `modified:<7d`, `mode:exhaustive`.

    Runs against the in-memory name index rather than a fresh `SELECT`, so a
    filter-only query does not re-materialise the whole files table.
    """
    index = nameindex.load(store)
    hits: list[Hit] = []
    kinds: list[str] = []
    for position in range(len(index)):
        path = index.path(position)
        kind = index.kind(position)
        size = int(index.sizes[position])
        mtime = float(index.mtimes[position])
        if since is not None and mtime < since:
            continue
        if not parsed.matches(path, kind, size, mtime):
            continue
        kinds.append(kind)
        hits.append(
            Hit(
                file_id=int(index.ids[position]),
                path=path,
                kind=kind,
                score=0.0,
                snippet="",
                name=index.name(position),
                ord=0,
                mtime=mtime,
                size=size,
                channels=("filter",),
            )
        )
    image_hits = [hit for hit in hits if hit.is_image]
    if image_hits:
        meta = store.image_meta([hit.file_id for hit in image_hits])
        for hit in image_hits:
            values = meta.get(hit.file_id)
            if values is not None:
                hit.width, hit.height, hit.phash = values
    # A pin has to survive a filter-only listing too, or a starred file would
    # lose its star the moment the user typed `type:pdf`.
    pins = store.pinned_ids()
    if pins:
        for hit in hits:
            hit.pinned = hit.file_id in pins
    hits.sort(key=_order_key(sort))
    return hits, _facets(kinds)


def _facets(kinds: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    return counts


# -------------------------------------------------------------------- search

def _files_from_chunks(store: Store, results: Sequence[tuple[int, float]]):
    """Collapse a chunk ranking to file ids, keeping the best chunk per file."""
    rows = store.chunk_rows([chunk_id for chunk_id, _ in results])
    ordered: list[int] = []
    seen: set[int] = set()
    best_chunk: dict[int, object] = {}
    for chunk_id, _ in results:
        row = rows.get(chunk_id)
        if row is None:
            continue
        file_id = int(row["file_id"])
        if file_id in seen:
            continue
        seen.add(file_id)
        ordered.append(file_id)
        best_chunk[file_id] = row
    return ordered, best_chunk


def _rank(
    store: Store,
    embedder,
    query: str | ParsedQuery,
    candidates: int = 50,
    k: int = RRF_K,
    kinds: Sequence[str] | None = None,
    folder: str | None = None,
    since: float | None = None,
    channels: Sequence[str] = ("bm25", "vector", "fuzzy"),
    weights: dict[str, float] | None = None,
    sort: str | None = None,
    parsed: ParsedQuery | None = None,
    usage: dict[int, tuple[int, float]] | None = None,
) -> tuple[list[Hit], dict[str, int]]:
    """Rank every matching file. Callers slice; `search_page` needs the total."""
    parsed = parsed or (query if isinstance(query, ParsedQuery) else understand(query))
    if kinds:
        parsed.kinds |= set(kinds)
    if folder:
        parsed.in_paths.append(folder)

    channel_weights = dict(RRF_WEIGHTS)
    channel_weights["vector"] = float(getattr(embedder, "rrf_weight", RRF_WEIGHTS["vector"]))
    if weights:
        channel_weights.update(weights)

    order = _resolve_sort(sort, parsed, listing=False)

    # No free text at all: the query is a pure filter, so list files directly.
    # `sym:` is a filter too, but a listing would answer it with every file in
    # the index, so it is excluded here and served by its own channel below.
    if parsed.mode == "exhaustive" or (
        not parsed.text and not parsed.regexes
        and not parsed.content_text and not parsed.symbols
    ):
        return _listing(store, parsed, _resolve_sort(sort, parsed, listing=True), since)

    pool = candidates * 4 if parsed.has_filters else candidates
    bm25_text = " ".join(part for part in (parsed.text, parsed.content_text) if part)
    fuzzy_text = parsed.text or parsed.name_text

    # Definitions, in rank order: exact name, then prefix, then substring. The
    # best symbol per file is kept for the snippet, because "this file defines
    # `parse_query` at line 214" is the answer, not the file name.
    symbol_rows = []
    for needle in parsed.symbols:
        symbol_rows.extend(store.find_symbols(needle, pool))
    symbol_files: list[int] = []
    symbol_best: dict[int, dict] = {}
    for row in symbol_rows:
        file_id = int(row["file_id"])
        if file_id in symbol_best:
            continue
        symbol_best[file_id] = {
            "name": row["name"], "kind": row["kind"],
            "line": int(row["line"]), "text": row["text"],
        }
        symbol_files.append(file_id)

    pinned = store.pinned_ids()
    match = build_match_query(bm25_text) if "bm25" in channels else None
    lexical = store.bm25(match, pool) if match else []
    # The CJK bigram table is queried with the same match query: CJK runs in
    # it arrive as bigram phrases, everything else simply matches nothing.
    cjk_lexical = store.bm25_cjk(match, pool) if match else []
    # The typo tier only pays its way when the exact tiers came back nearly
    # empty — an OR over common 3-grams on a large index costs ~100 ms, so a
    # threshold any looser would tax ordinary queries to reproduce hits bm25
    # already has. Its pool is small for the same reason.
    tri = []
    if (
        "bm25" in channels
        and store.has_trigram
        and len(lexical) < 3
        and (tri_match := build_tri_query(bm25_text)) is not None
    ):
        tri = store.bm25_tri(tri_match, min(pool, 25))
    semantic = (
        store.knn(embedder.encode_query(bm25_text or parsed.text), pool)
        if "vector" in channels and (bm25_text or parsed.text)
        else []
    )
    fuzzy = fuzzy_files(store, fuzzy_text, pool) if "fuzzy" in channels and fuzzy_text else []
    regex_rows = []
    if parsed.regexes:
        regex_rows = store.regex_chunks("|".join(f"(?:{r})" for r in parsed.regexes), pool * 2)

    # No lexical match, no fuzzy match and a non-semantic embedder means every
    # vector "hit" is noise: hashed features have no notion of meaning, so a
    # query like «кофемашина» matches spinner scripts on hash collisions alone.
    if (
        "bm25" in channels
        and not lexical
        and not cjk_lexical
        and not tri
        and not fuzzy
        and not regex_rows
        and not symbol_files
        and not getattr(embedder, "semantic", False)
    ):
        return [], {}

    lexical_files, chunk_rows = _files_from_chunks(store, lexical)
    cjk_files, cjk_chunk_rows = _files_from_chunks(store, cjk_lexical)
    chunk_rows.update(cjk_chunk_rows)
    tri_files, tri_chunk_rows = _files_from_chunks(store, tri)
    tri_files = _verify_tri(tri_files, tri_chunk_rows, bm25_text)
    chunk_rows.update(tri_chunk_rows)
    semantic_files, semantic_rows = _files_from_chunks(store, semantic)
    chunk_rows.update(semantic_rows)
    regex_files, regex_chunk_rows = _files_from_chunks(
        store, [(int(row["id"]), 0.0) for row in regex_rows]
    )
    chunk_rows.update(regex_chunk_rows)

    # File metadata for every candidate, including fuzzy-only files that have no
    # chunk in the pool.
    file_rows = {int(row["file_id"]): row for row in chunk_rows.values()}
    missing = {
        file_id
        for file_id in (
            *lexical_files, *cjk_files, *tri_files, *semantic_files, *fuzzy_files_ids(fuzzy),
            *regex_files, *symbol_files,
        )
        if file_id not in file_rows
    }
    for row in store.file_rows_by_ids(missing):
        file_rows[int(row["id"])] = {
            "path": row["path"], "kind": row["kind"], "size": row["size"],
            "mtime": row["mtime"], "file_id": row["id"],
        }

    # `in:` pushdown: the scope is resolved against the in-memory path index
    # (one C-level byte scan) and out-of-scope files are dropped before fusion.
    # Without this a narrow folder burns pool slots on files `matches()` would
    # reject anyway, and a `type:`-only query inside a deep scope could miss
    # everything below rank 50.
    scope_ids: set[int] | None = None
    if parsed.in_paths:
        scope_ids = scoped_file_ids(store, parsed.in_paths)
        if not scope_ids:
            scope_ids = set()
        ranked_scope = {
            "bm25": lexical_files, "cjk": cjk_files, "tri": tri_files,
            "vector": semantic_files, "fuzzy": fuzzy_files_ids(fuzzy),
            "regex": regex_files, "symbol": symbol_files,
        }
        for name, files in ranked_scope.items():
            ranked_scope[name] = [fid for fid in files if fid in scope_ids]
        lexical_files = ranked_scope["bm25"]
        cjk_files = ranked_scope["cjk"]
        tri_files = ranked_scope["tri"]
        semantic_files = ranked_scope["vector"]

    def allowed(file_id: int) -> bool:
        row = file_rows.get(file_id)
        if row is None:
            return False
        if since is not None and float(row["mtime"]) < since:
            return False
        return parsed.matches(row["path"], row["kind"], int(row["size"]), float(row["mtime"]))

    # Facets over every candidate that survives the filters — not just the
    # sliced page — so `type:` tabs can show real counts, the way kensaku
    # counts its matched set.
    allowed_ids = {fid for fid in file_rows if allowed(fid)}
    kind_facets: dict[str, int] = {}
    for fid in allowed_ids:
        kind = file_rows[fid]["kind"]
        kind_facets[kind] = kind_facets.get(kind, 0) + 1

    # Did-you-mean: the trigram tier found files for a query the exact tiers
    # could not read — one of its chunks probably spells the word right.
    if not cjk_lexical and tri and bm25_text:
        # The typo tier only runs on thin-lexical queries, so a suggestion
        # here is a correction of a degraded query, not noise on a good one.
        suggestion = _suggest_term(bm25_text, tri_chunk_rows)
        if suggestion:
            parsed.hints.append(Hint("did_you_mean", suggestion))

    ranked = {
        "bm25": lexical_files,
        "cjk": cjk_files,
        "tri": tri_files,
        "vector": semantic_files,
        "fuzzy": fuzzy_files_ids(fuzzy),
        "regex": regex_files,
        "symbol": symbol_files,
    }
    fused: dict[int, float] = {}
    matched_channels: dict[int, list[str]] = {}
    for name, files in ranked.items():
        if not files:
            continue
        weight = channel_weights.get(name, 1.0)
        rank = 0
        for file_id in files:
            if file_id not in allowed_ids:
                continue
            rank += 1
            fused[file_id] = fused.get(file_id, 0.0) + weight / (k + rank)
            matched_channels.setdefault(file_id, []).append(name)

    max_rrf = sum(
        channel_weights.get(name, 1.0) / (k + 1) for name, files in ranked.items() if files
    ) or 1.0

    now = time.time()
    usage = usage if usage is not None else {}
    hits: list[Hit] = []
    for file_id, score in fused.items():
        row = file_rows[file_id]
        name = basename(row["path"])
        base = score / max_rrf
        hits.append(
            Hit(
                file_id=file_id,
                path=row["path"],
                kind=row["kind"],
                score=round(finalize_score(
                    base, name, row["path"], float(row["mtime"]), now,
                    usage.get(file_id, (0, 0.0)), parsed.text or parsed.name_text,
                    pinned=file_id in pinned,
                ), 6),
                snippet=symbol_snippet(symbol_best.get(file_id))
                if file_id in symbol_best
                else (make_snippet(chunk_rows[file_id]["text"], bm25_text or parsed.text)
                      if file_id in chunk_rows else ""),
                name=name,
                ord=int(chunk_rows[file_id]["ord"]) if file_id in chunk_rows else 0,
                mtime=float(row["mtime"]),
                size=int(row["size"]),
                channels=tuple(matched_channels.get(file_id, [])),
                pinned=file_id in pinned,
                symbol=(
                    f"{symbol_best[file_id]['kind']} {symbol_best[file_id]['name']}"
                    if file_id in symbol_best else ""
                ),
            )
        )

    for hit in hits:
        if hit.is_image:
            image = store.image_by_file_id(hit.file_id)
            if image is not None:
                hit.width = int(image["width"] or 0)
                hit.height = int(image["height"] or 0)
                hit.phash = int(image["phash"] or 0)

    hits.sort(key=_order_key(order))
    return hits, kind_facets


def symbol_snippet(symbol: dict | None) -> str:
    """One line: what was defined, where, and the definition itself."""
    if not symbol:
        return ""
    return f"{symbol['kind']} {symbol['name']} · line {symbol['line']} · {symbol['text']}"


def fuzzy_files_ids(results: Sequence[tuple[int, float]]) -> list[int]:
    return [file_id for file_id, _ in results]


def hybrid_search(
    store: Store,
    embedder,
    query: str | ParsedQuery,
    limit: int = 10,
    candidates: int = 50,
    k: int = RRF_K,
    kinds: Sequence[str] | None = None,
    folder: str | None = None,
    since: float | None = None,
    channels: Sequence[str] = ("bm25", "vector", "fuzzy"),
    weights: dict[str, float] | None = None,
    offset: int = 0,
    sort: str | None = None,
    parsed: ParsedQuery | None = None,
    usage: dict[int, tuple[int, float]] | None = None,
) -> list[Hit]:
    """One page of ranked files. `_rank` does the work; this slices the page."""
    hits, _ = _rank(
        store, embedder, query, candidates=candidates, k=k, kinds=kinds, folder=folder,
        since=since, channels=channels, weights=weights, sort=sort, parsed=parsed, usage=usage,
    )
    return hits[offset:offset + limit]


def search_page(
    store: Store,
    embedder,
    query: str,
    limit: int = 12,
    offset: int = 0,
    sort: str | None = None,
    parsed: ParsedQuery | None = None,
    usage: dict[int, tuple[int, float]] | None = None,
    group: bool = True,
    applied: list | None = None,
) -> SearchPage:
    """One query, with the counts and chips the UI needs to explain itself.

    `applied` is the list of dictionary entries the caller already expanded, for
    the clients that build their own `ParsedQuery` (the UI merges filter chips
    into it, the CLI pre-parses). Without it a caller that pre-parses would
    silently lose the expansion, and an alias that works in one client and not
    another is worse than no aliases at all.
    """
    if parsed is None:
        # The dictionary is expanded here rather than in the API because the CLI
        # and the MCP server search through this function too.
        query, applied = expand_aliases(store, query)
    applied = applied or []
    parsed = parsed or understand(query)
    listing = parsed.mode == "exhaustive" or (
        not parsed.text and not parsed.regexes and not parsed.content_text
    )
    resolved = _resolve_sort(sort, parsed, listing=listing)
    hits, facets = _rank(store, embedder, parsed, sort=resolved, parsed=parsed, usage=usage)
    total = len(hits)

    collapsed = 0
    if group:
        # One batched hash lookup for the page: without it two byte-identical
        # copies of the same file occupy two rows in every result list.
        grouped = group_hits(hits, hashes=store.sha256_for(hit.file_id for hit in hits))
        collapsed = sum(item.size - 1 for item in grouped)
        hits = []
        for item in grouped:
            lead = item.representative
            lead.group_size = item.size
            lead.group_reason = item.reason if item.size > 1 else ""
            lead.group_members = tuple(member.file_id for member in item.members)
            hits.append(lead)
        total = len(hits)

    return SearchPage(
        hits=hits[offset:offset + limit],
        total=total,
        facets=facets,
        chips=[item.as_dict() for item in applied] + [chip.as_dict() for chip in parsed.chips],
        hints=[hint.as_dict() for hint in parsed.hints],
        query=query,
        mode=parsed.mode,
        sort=resolved,
        offset=offset,
        limit=limit,
        approx=not listing,
        groups=len(hits),
        collapsed=collapsed,
    )


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
    """Collapse hits into a per-file ranking, keeping the best score."""
    best: dict[str, float] = {}
    for hit in hits:
        best[hit.path] = max(best.get(hit.path, 0.0), hit.score)
    return sorted(best.items(), key=lambda item: -item[1])
