"""Move a whole index between machines as one archive.

An index is more than `index.db`: the vector pack, the name-index sidecar and
the sealed BM25 sidecar live next to it, and an export that forgets them costs
the receiving machine a full rebuild of each. Everything travels in one zip
with a manifest that carries every entry's SHA-256, so an interrupted download
is caught before it can corrupt a working index.

The vault key is deliberately *not* in the archive. A sealed index without its
key is just ciphertext — which is exactly what an archive left in a shared
folder should be. `autofind seal status` on the receiving machine says what to
do next (copy vault.key over, or set AUTOFIND_PASSPHRASE for passphrase
vaults).

The database is snapshotted with SQLite's backup API, so a running app with a
hot WAL still exports a consistent copy; the live files are never opened for
writing by this module.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path

from .__version__ import __version__
from .store import SCHEMA_VERSION
from .vectors import hot_path, pack_path

MANIFEST_NAME = "manifest.json"
DB_NAME = "index.db"
MAGIC = b"AFXP"  # archive marker, first line of the manifest


def sidecar_paths(db_path: str | Path) -> list[Path]:
    """Every live sidecar of this index: pack, hot delta, name index, BM25."""
    db_path = Path(db_path)
    candidates = [pack_path(db_path), hot_path(db_path), Path(str(db_path) + ".nameidx")]
    # The name index companion carries a content fingerprint in its name.
    try:
        candidates += [p for p in db_path.parent.glob(db_path.name + ".nameidx.*.bin")]
    except OSError:
        pass
    candidates += [Path(str(db_path) + ".bm25seal")]
    return [p for p in candidates if p.is_file()]


def _entry_name(db_path: Path, source: Path) -> str:
    """The archive name of a sidecar, always relative to `index.db`.

    A custom-named database exports sidecars named after itself ("my.db.v4.vec");
    the receiving machine's index is always `index.db`, so the stem is
    normalised on the way in.
    """
    suffix = source.name[len(db_path.name):]
    return DB_NAME + suffix


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _snapshot(db_path: str | Path, dest: Path) -> dict:
    """A consistent copy of the database plus what the index contains."""
    src = sqlite3.connect(f"file:{Path(db_path).as_posix()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    conn = sqlite3.connect(str(dest))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'sealed'"
        ).fetchone()
        dim_row = conn.execute("SELECT MAX(dim) FROM file_vectors").fetchone()
        return {
            "sealed": bool(row and row[0] == "1"),
            "dim": int(dim_row[0]) if dim_row and dim_row[0] else 0,
            "files": int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]),
            "chunks": int(conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]),
            "schema": int(conn.execute("PRAGMA user_version").fetchone()[0]),
        }
    finally:
        conn.close()


def export_index(db_path: str | Path, archive: str | Path) -> dict:
    """Write the index and its sidecars to a zip archive; returns the manifest."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(f"no index at {db_path}")
    archive = Path(archive)
    archive.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="af-export-") as tmp:
        snapshot = Path(tmp) / DB_NAME
        info = _snapshot(db_path, snapshot)
        manifest = {
            "magic": MAGIC.decode("ascii"),
            "app": __version__,
            "schema": SCHEMA_VERSION,
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **info,
            "entries": {},
        }
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(snapshot, DB_NAME)
            manifest["entries"][DB_NAME] = _sha256(snapshot)
            for source in sidecar_paths(db_path):
                name = _entry_name(db_path, source)
                zf.write(source, name)
                manifest["entries"][name] = _sha256(source)
            zf.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    return manifest


def _verify(zip_path: Path) -> dict:
    """Open the archive and check every entry against the manifest hashes."""
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(f"{zip_path} is not an AutoFind archive: {exc}") from exc
    with zf:
        names = set(zf.namelist())
        if MANIFEST_NAME not in names or DB_NAME not in names:
            raise ValueError(f"{zip_path} does not look like an index archive")
        manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
        if manifest.get("magic") != MAGIC.decode("ascii"):
            raise ValueError(f"{zip_path} is not an AutoFind index archive")
        for name, digest in manifest.get("entries", {}).items():
            if name not in names:
                raise ValueError(f"archive is missing {name}")
            actual = hashlib.sha256(zf.read(name)).hexdigest()
            if actual != digest:
                raise ValueError(
                    f"{name} is corrupt (sha256 mismatch); re-export the index"
                )
        return manifest


def import_index(archive: str | Path, home: str | Path, force: bool = False) -> dict:
    """Unpack a verified archive into `home`; returns the manifest.

    An existing index is never clobbered silently: the call refuses until
    `force` is set, and then keeps the old files as `<db>.import-backup/`.
    """
    archive = Path(archive)
    home = Path(home)
    manifest = _verify(archive)
    if manifest.get("schema", SCHEMA_VERSION) > SCHEMA_VERSION:
        raise ValueError(
            f"the archive was made by a newer AutoFind "
            f"(schema {manifest['schema']} > {SCHEMA_VERSION}); update this app first"
        )

    db_path = home / "index.db"
    if db_path.exists():
        if not force:
            raise FileExistsError(
                f"an index already exists at {db_path}; pass --force to replace it"
            )
        backup = home / "import-backup"
        backup.mkdir(exist_ok=True)
        # -wal and -shm must move with the db: leaving them beside a fresh
        # index.db lets SQLite apply a foreign WAL and corrupt the import.
        stale = sidecar_paths(db_path) + [db_path,
                                          Path(str(db_path) + "-wal"),
                                          Path(str(db_path) + "-shm")]
        for existing in stale:
            if existing.is_file():
                existing.replace(backup / existing.name)

    home.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        for name in manifest["entries"]:
            target = home / name
            # The names come from the archive, so keep the write inside home.
            if target.parent.resolve() != home.resolve():
                raise ValueError(f"unsafe entry name in archive: {name}")
            zf.extract(name, home)
    return manifest


def import_status(manifest: dict) -> str:
    """One human line about what arrived and what it needs."""
    lines = [
        f"index imported: {manifest.get('files', 0)} files, "
        f"{manifest.get('chunks', 0)} chunks, schema {manifest.get('schema')}"
    ]
    if manifest.get("sealed"):
        lines.append(
            "the index is sealed: copy vault.key to the same folder (or set "
            "AUTOFIND_PASSPHRASE for passphrase vaults) or content stays unreadable"
        )
    return " | ".join(lines)
