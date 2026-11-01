"""Path-level privacy guard.

"Nothing leaves the machine" is a promise about the network, not about the
index. OCR turns a screenshot of a password manager into plain text inside an
unencrypted SQLite file, and a full-disk scan happily swallows key material,
bank statements and medical records. So sensitive locations are dropped before
any text is read, and the drop is counted and reported instead of being silent.

This is a filter, not encryption: a file that does not match these patterns is
still stored in the clear. `docs/privacy.md` says so explicitly.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

# Directory names that never belong in a search index. Lowercase, matched
# exactly against each path component so a project folder called "keys" inside
# a repository is not caught by accident -- only the well-known names are.
SENSITIVE_DIRS = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".password-store",
        "1password",
        "bitwarden",
        "keepass",
        "keepassxc",
        "keychain",
        "wallets",
        "electrum",
        "ledger",
        "passwords",
        "credentials",
        "secrets",
        "private keys",
        "bank statements",
        "medical records",
    }
)

# Filename globs. Deliberately narrow: "*.key" would match a translation file,
# so the risky extensions are listed explicitly.
SENSITIVE_FILES = (
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "*.kdbx",
    "*.kdb",
    "*.keychain",
    "*.keychain-db",
    "*.pfx",
    "*.p12",
    "*.keystore",
    "*password*",
    "*passwd*",
    "*secret*",
    "*credential*",
    "*recovery*code*",
    "*seed*phrase*",
    "*.env",
    "wallet.dat",
)

# Full path fragments, checked as a lowercase substring. These are the places
# where the operating system and browsers keep session material.
SENSITIVE_FRAGMENTS = (
    "appdata/roaming/microsoft/crypto",
    "appdata/local/google/chrome/user data",
    "appdata/roaming/mozilla/firefox/profiles",
    "appdata/local/microsoft/edge/user data",
    "/library/keychains",
    "/.config/google-chrome",
    "/.config/chromium",
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


def match_reason(path: str | Path) -> str | None:
    """Return why a path is sensitive, or None when it is fine to index."""
    text = _normalise(path)
    parts = [part for part in text.split("/") if part]
    if not parts:
        return None

    for part in parts[:-1]:
        if part in SENSITIVE_DIRS:
            return f"directory '{part}'"

    name = parts[-1]
    suffix = Path(name).suffix.lower()
    for pattern in SENSITIVE_FILES:
        if pattern in BROAD_NAME_GLOBS and suffix in CODE_SUFFIXES:
            continue
        if fnmatch.fnmatch(name, pattern):
            return f"filename matches '{pattern}'"

    for fragment in SENSITIVE_FRAGMENTS:
        if fragment in text:
            return f"path contains '{fragment}'"
    return None


def is_sensitive(path: str | Path) -> bool:
    return match_reason(path) is not None


def describe() -> dict:
    """Machine readable summary for `doctor` and /api/status."""
    return {
        "sensitive_dirs": sorted(SENSITIVE_DIRS),
        "sensitive_files": list(SENSITIVE_FILES),
        "sensitive_fragments": list(SENSITIVE_FRAGMENTS),
    }


def indexed_sensitive(store, limit: int | None = None) -> list[dict]:
    """Files already in the index that the filter would now refuse."""
    found: list[dict] = []
    for row in store.conn.execute("SELECT id, path FROM files ORDER BY path").fetchall():
        reason = match_reason(row["path"])
        if reason:
            found.append({"file_id": int(row["id"]), "path": row["path"], "reason": reason})
            if limit is not None and len(found) >= limit:
                break
    return found


def purge_indexed(store) -> dict:
    """Drop sensitive files that were indexed before the filter existed."""
    found = indexed_sensitive(store)
    for item in found:
        store.drop_file(item["file_id"])
    store.commit()
    return {"removed": len(found), "files": [item["path"] for item in found]}
