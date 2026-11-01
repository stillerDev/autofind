"""The personal layer: pins, saved searches and the personal dictionary.

Three things kensaku has that a search engine does not get for free, and the
three things that make a switch expensive if they do not come along: the files
you keep on the empty screen, the queries you run every week, and the words you
type instead of the ones the files contain.

They live in the same SQLite file as the index, in `pins`, `saved` and `aliases`,
which means they travel with a backup, they are readable with any SQLite client,
and they never leave the machine. That is the whole argument against a cloud
favourites list, and it is also why `semfind import kensaku` can move them over
in one step instead of asking anyone to retype them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .store import Store

# A dictionary with a hundred entries is a dictionary nobody reads. Expansion is
# capped so a pathological alias (`a → a b`) cannot grow a query without bound.
MAX_EXPANSIONS = 8
MAX_EXPANDED_CHARS = 600


@dataclass(frozen=True, slots=True)
class Applied:
    """One alias that actually fired, for the chip row."""

    alias: str
    expand: str

    def as_dict(self) -> dict:
        return {
            "kind": "alias",
            "value": f"{self.alias} → {self.expand}",
            "label": f"{self.alias} → {self.expand}",
            "raw": self.alias,
            "source": self.alias,
        }


def expand_aliases(store: Store, text: str) -> tuple[str, list[Applied]]:
    """Replace dictionary words with what they stand for.

    Whole words only, case-insensitively, and never inside a `key:value` pair:
    `type:cv` is a filter for `.cv` files, not a request to expand `cv`. The
    replacement is a plain substitution into the query, which is what makes the
    result explainable — the chips row shows exactly what was replaced.
    """
    table = store.aliases()
    if not table or not text:
        return text, []

    applied: list[Applied] = []
    out = text
    for alias, expand in sorted(table.items(), key=lambda item: -len(item[0])):
        if len(applied) >= MAX_EXPANSIONS:
            break
        # `(?<![\w:-])` keeps the alias out of `type:cv` and `re:cv`; the
        # trailing guard keeps `cv` from firing inside `cvs`.
        pattern = re.compile(rf"(?<![\w:-]){re.escape(alias)}(?![\w-])", re.IGNORECASE)
        if not pattern.search(out):
            continue
        out = pattern.sub(expand, out)
        applied.append(Applied(alias, expand))
        if len(out) > MAX_EXPANDED_CHARS:
            break
    return out, applied


def pin(store: Store, file_id: int, pinned: bool = True) -> bool:
    """Pin or unpin one file. Returns the resulting state."""
    return store.set_pin(file_id, pinned)


def toggle_pin(store: Store, file_id: int) -> bool:
    return store.set_pin(file_id, file_id not in store.pinned_ids())


def save(store: Store, query: str, label: str = "") -> dict:
    """Remember a query. Saving the same one twice is a rename, not a duplicate."""
    query = " ".join(query.split())
    if not query:
        raise ValueError("empty query")
    saved_id = store.save_search(query, label or query)
    store.commit()
    return {"id": saved_id, "query": query, "label": label or query}


def drop(store: Store, saved_id: int) -> bool:
    gone = store.drop_search(saved_id)
    store.commit()
    return gone


def teach(store: Store, alias: str, expand: str) -> dict:
    """Add or replace one dictionary entry."""
    alias = alias.strip().lower()
    expand = " ".join(expand.split())
    if not alias or not expand:
        raise ValueError("alias and expansion are both required")
    if " " in alias:
        raise ValueError("an alias is one word")
    store.set_alias(alias, expand)
    store.commit()
    return {"alias": alias, "expand": expand}


def forget(store: Store, alias: str) -> bool:
    gone = store.drop_alias(alias.strip().lower())
    store.commit()
    return gone
