"""CJK-aware indexing helpers.

SQLite's unicode61 tokenizer splits on whitespace and punctuation, and Chinese,
Japanese and unspaced Korean have neither inside a sentence. A run like 数据库
索引 becomes ONE token, and a query for 索引 then matches nothing. The fix is
the classic CJK bigram: at index time each contiguous CJK run is rewritten as
space-separated overlapping pairs (`数据库索引` -> `数据 据库 库索 索引`), and
at query time a run becomes a phrase of the same pairs, so adjacency — the only
thing bigrams preserve — is checked by FTS5 itself.

Only the full-text index sees the transformed text: since schema v9 the
bigrams are written into the single contentless FTS table alongside the latin
words, so there is no separate CJK copy of the corpus. The stored document text
and every snippet stay exactly as written.
"""

from __future__ import annotations

import re

# Han (incl. ext-A and compatibility), kana, hangul syllables and jamo. CJK
# punctuation is deliberately left out: unicode61 already breaks tokens there.
CJK_RE = re.compile(
    "[\u1100-\u11ff\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    "\uac00-\ud7af\u3130-\u318f]+"
)


def has_cjk(text: str) -> bool:
    return CJK_RE.search(text) is not None


def cjk_bigrams(text: str) -> str:
    """Index-side form: every CJK run rewritten as overlapping bigrams.

    A single character has no pair; it is kept as its own token so one-char
    queries still have something to find. Each run is padded with spaces so a
    latin neighbour (`CUDA数据库`) cannot swallow the first bigram into one
    unicode61 token.
    """
    if not CJK_RE.search(text):
        return text

    def run(match: re.Match) -> str:
        run_text = match.group(0)
        if len(run_text) == 1:
            return f" {run_text} "
        return " " + " ".join(run_text[i : i + 2] for i in range(len(run_text) - 1)) + " "

    # The padding spaces are the point: without them `CUDA数据库` stays one
    # unicode61 token and a 数据 phrase can never match inside it. Double
    # spaces are harmless to FTS.
    return re.sub(r"  +", " ", CJK_RE.sub(run, text)).strip()


def cjk_query_phrase(run: str) -> str | None:
    """Query-side phrase for one CJK run, or None for a single character.

    `数据库索引` -> `"数据 据库 库索 索引"`: an FTS5 phrase over the same
    bigrams the index stored, which enforces the original character order.
    """
    if len(run) < 2:
        return None
    return " ".join(run[i : i + 2] for i in range(len(run) - 1))
