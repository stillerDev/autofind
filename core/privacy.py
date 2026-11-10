"""Path-level privacy guard.

"Nothing leaves the machine" is a promise about the network, not about the
index. OCR turns a screenshot of a password manager into plain text inside an
unencrypted SQLite file, and a full-disk scan happily swallows key material,
bank statements and medical records. So sensitive locations are dropped before
any text is read, and the drop is counted and reported instead of being silent.

The patterns are grouped into categories a person can reason about and switch
off one by one ("I do want my SSH keys searchable, but not browser sessions").
With every category on, the guard behaves exactly like the old single filter.

This is a filter, not encryption: a file that does not match these patterns is
still stored in the clear. `docs/privacy.md` says so explicitly.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PrivacyCategory:
    """One switchable group of sensitive paths."""

    id: str
    dirs: frozenset[str] = frozenset()
    files: tuple[str, ...] = ()
    fragments: tuple[str, ...] = ()


# The seven groups the panel lists. Every pattern that used to live in the flat
# SENSITIVE_* tuples is in exactly one of them; the unions below are derived, so
# the default behaviour cannot drift from the category view.
CATEGORIES: tuple[PrivacyCategory, ...] = (
    PrivacyCategory(
        id="keys",
        dirs=frozenset({".ssh", ".gnupg", ".aws", ".azure", ".kube", "private keys"}),
        files=("id_rsa*", "id_ed25519*", "id_ecdsa*", "*.pfx", "*.p12", "*.keystore"),
        fragments=("appdata/roaming/microsoft/crypto",),
    ),
    PrivacyCategory(
        id="passwords",
        dirs=frozenset(
            {
                ".password-store",
                "1password",
                "bitwarden",
                "keepass",
                "keepassxc",
                "keychain",
                "passwords",
                "credentials",
            }
        ),
        files=("*.kdbx", "*.kdb", "*.keychain", "*.keychain-db"),
        fragments=("/library/keychains",),
    ),
    PrivacyCategory(
        id="wallets",
        dirs=frozenset({"wallets", "electrum", "ledger"}),
        files=("wallet.dat",),
    ),
    PrivacyCategory(
        id="browser",
        fragments=(
            "appdata/local/google/chrome/user data",
            "appdata/roaming/mozilla/firefox/profiles",
            "appdata/local/microsoft/edge/user data",
            "/.config/google-chrome",
            "/.config/chromium",
        ),
    ),
    PrivacyCategory(id="finance", dirs=frozenset({"bank statements"})),
    PrivacyCategory(id="medical", dirs=frozenset({"medical records"})),
    PrivacyCategory(
        id="secrets",
        dirs=frozenset({"secrets"}),
        files=(
            "*password*",
            "*passwd*",
            "*secret*",
            "*credential*",
            "*recovery*code*",
            "*seed*phrase*",
            "*.env",
        ),
    ),
)

DEFAULT_CATEGORIES = frozenset(category.id for category in CATEGORIES)

# Flat unions, kept for callers that only need "is this name sensitive at all"
# and for `doctor` / API summaries.
SENSITIVE_DIRS = frozenset().union(*(category.dirs for category in CATEGORIES))
SENSITIVE_FILES = tuple(
    pattern for category in CATEGORIES for pattern in category.files
)
SENSITIVE_FRAGMENTS = tuple(
    fragment for category in CATEGORIES for fragment in category.fragments
)

# Broad name globs are skipped for source code: blender ships `secrets.py`, and
# indexing a standard-library module is not a privacy incident. Explicit
# extensions and path fragments above still apply to code.
CODE_SUFFIXES = frozenset(
    {
        ".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt",
        ".c", ".h", ".cpp", ".hpp", ".cs", ".rb", ".php", ".sh", ".ps1", ".lua",
    }
)
BROAD_NAME_GLOBS = frozenset({"*password*", "*passwd*", "*secret*", "*credential*"})


def _normalise(path: str | Path) -> str:
    return str(path).replace("\\", "/").lower()


def _enabled(categories: frozenset[str] | set[str] | None) -> frozenset[str]:
    if categories is None:
        return DEFAULT_CATEGORIES
    return frozenset(categories)


def sensitive_dirs(categories: frozenset[str] | set[str] | None = None) -> frozenset[str]:
    """Directory names to prune for the categories that are switched on."""
    active = _enabled(categories)
    return frozenset().union(
        *(category.dirs for category in CATEGORIES if category.id in active)
    )


def match_reason(
    path: str | Path, categories: frozenset[str] | set[str] | None = None
) -> str | None:
    """Return why a path is sensitive, or None when it is fine to index.

    `categories` selects which groups apply; None means all of them.
    """
    active = _enabled(categories)
    text = _normalise(path)
    parts = [part for part in text.split("/") if part]
    if not parts:
        return None

    for part in parts[:-1]:
        for category in CATEGORIES:
            if category.id in active and part in category.dirs:
                return f"directory '{part}'"

    name = parts[-1]
    suffix = Path(name).suffix.lower()
    for category in CATEGORIES:
        if category.id not in active:
            continue
        for pattern in category.files:
            if pattern in BROAD_NAME_GLOBS and suffix in CODE_SUFFIXES:
                continue
            if fnmatch.fnmatch(name, pattern):
                return f"filename matches '{pattern}'"

    for category in CATEGORIES:
        if category.id not in active:
            continue
        for fragment in category.fragments:
            if fragment in text:
                return f"path contains '{fragment}'"
    return None


def is_sensitive(path: str | Path, categories: frozenset[str] | set[str] | None = None) -> bool:
    return match_reason(path, categories) is not None


def describe() -> dict:
    """Machine readable summary for `doctor` and /api/status."""
    return {
        "sensitive_dirs": sorted(SENSITIVE_DIRS),
        "sensitive_files": list(SENSITIVE_FILES),
        "sensitive_fragments": list(SENSITIVE_FRAGMENTS),
        "categories": [
            {
                "id": category.id,
                "dirs": sorted(category.dirs),
                "files": list(category.files),
                "fragments": list(category.fragments),
            }
            for category in CATEGORIES
        ],
    }


def indexed_sensitive(
    store,
    limit: int | None = None,
    categories: frozenset[str] | set[str] | None = None,
) -> list[dict]:
    """Files already in the index that the filter would now refuse."""
    found: list[dict] = []
    for row in store.conn.execute("SELECT id, path FROM files ORDER BY path").fetchall():
        reason = match_reason(row["path"], categories)
        if reason:
            found.append({"file_id": int(row["id"]), "path": row["path"], "reason": reason})
            if limit is not None and len(found) >= limit:
                break
    return found


def purge_indexed(
    store, categories: frozenset[str] | set[str] | None = None
) -> dict:
    """Drop sensitive files that were indexed before the filter existed."""
    found = indexed_sensitive(store, categories=categories)
    for item in found:
        store.drop_file(item["file_id"])
    store.commit()
    return {"removed": len(found), "files": [item["path"] for item in found]}
