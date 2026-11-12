"""Index transfer: export packs db + sidecars, import restores a working index."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from core import transfer
from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.scanner import dedupe_roots
from core.search import hybrid_search
from core.store import Store


def _build_index(tmp: str) -> Path:
    root = Path(tmp) / "corpus"
    root.mkdir()
    (root / "notes.md").write_text(
        "переносимый индекс: hybrid search объединяет BM25 и векторы", encoding="utf-8"
    )
    (root / "tool.py").write_text("def run():\n    return 42\n", encoding="utf-8")
    config = IndexConfig(home=Path(tmp) / "home")
    embedder = HashingEmbedder()
    store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
    run_index(dedupe_roots([root]), config, store, embedder)
    store.close()
    return Path(tmp) / "index.db"


class TransferTests(unittest.TestCase):
    def test_export_creates_manifest_and_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(tmp)
            archive = Path(tmp) / "out" / "index.zip"
            manifest = transfer.export_index(db, archive)
            self.assertEqual(manifest["magic"], "AFXP")
            self.assertIn("index.db", manifest["entries"])
            with zipfile.ZipFile(archive) as zf:
                names = set(zf.namelist())
            self.assertIn("index.db", names)
            self.assertIn("manifest.json", names)

    def test_live_sidecars_travel_with_the_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(tmp)
            # sidecars a running app leaves behind: vector pack, name index,
            # its mmapped companion and the sealed-BM25 cache
            for suffix in (".v4.vec", ".v4.hot", ".nameidx", ".nameidx.abc12.bin", ".bm25seal"):
                Path(str(db) + suffix).write_bytes(b"sidecar " + suffix.encode())
            archive = Path(tmp) / "index.zip"
            manifest = transfer.export_index(db, archive)
            self.assertIn("index.db.v4.vec", manifest["entries"])
            self.assertIn("index.db.nameidx.abc12.bin", manifest["entries"])
            self.assertIn("index.db.bm25seal", manifest["entries"])
            with tempfile.TemporaryDirectory() as dest:
                home = Path(dest)
                transfer.import_index(archive, home, force=True)
                for suffix in (".v4.vec", ".v4.hot", ".nameidx", ".nameidx.abc12.bin", ".bm25seal"):
                    self.assertTrue((home / ("index.db" + suffix)).is_file())

    def test_import_restores_a_searchable_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(tmp)
            archive = Path(tmp) / "index.zip"
            transfer.export_index(db, archive)

            with tempfile.TemporaryDirectory() as dest:
                home = Path(dest) / "home"
                manifest = transfer.import_index(archive, home)
                self.assertGreater(manifest["files"], 0)
                restored = home / "index.db"
                self.assertTrue(restored.is_file())

                embedder = HashingEmbedder()
                store = Store(restored, embedder.name, embedder.dim, readonly=True)
                try:
                    hits = hybrid_search(store, embedder, "hybrid search", limit=5)
                    self.assertTrue(hits)
                    self.assertIn("notes.md", hits[0].path)
                finally:
                    store.close()

    def test_import_refuses_to_clobber_without_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(tmp)
            archive = Path(tmp) / "index.zip"
            transfer.export_index(db, archive)
            with tempfile.TemporaryDirectory() as dest:
                home = Path(dest)
                (home / "index.db").write_bytes(b"existing index")
                with self.assertRaises(FileExistsError):
                    transfer.import_index(archive, home)
                # force moves the old files aside instead of losing them
                transfer.import_index(archive, home, force=True)
                backup = home / "import-backup" / "index.db"
                self.assertEqual(backup.read_bytes(), b"existing index")

    def test_corrupt_entry_is_caught_by_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(tmp)
            archive = Path(tmp) / "index.zip"
            transfer.export_index(db, archive)
            # damage one byte inside the db entry, keeping the zip readable
            with zipfile.ZipFile(archive) as zf:
                data = {n: zf.read(n) for n in zf.namelist()}
            data["index.db"] = data["index.db"][:-4] + b"\x00" * 4
            with zipfile.ZipFile(archive, "w") as zf:
                for name, blob in data.items():
                    zf.writestr(name, blob)
            with self.assertRaises(ValueError):
                transfer.import_index(archive, Path(tmp) / "home")

    def test_newer_schema_archive_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = _build_index(tmp)
            archive = Path(tmp) / "index.zip"
            transfer.export_index(db, archive)
            with zipfile.ZipFile(archive) as zf:
                data = {n: zf.read(n) for n in zf.namelist()}
            import json

            manifest = json.loads(data["manifest.json"])
            manifest["schema"] = transfer.SCHEMA_VERSION + 1
            data["manifest.json"] = json.dumps(manifest)
            with zipfile.ZipFile(archive, "w") as zf:
                for name, blob in data.items():
                    zf.writestr(name, blob)
            with self.assertRaises(ValueError):
                transfer.import_index(archive, Path(tmp) / "home")


if __name__ == "__main__":
    unittest.main()
