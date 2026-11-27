"""Honest indexing progress and a sidecar the live server can actually replace.

The progress bar used to read 100% while the run was still finishing: the
skip phase raced through unchanged files, and the scan walk plus the final
seal ran with no percent at all. The sealed BM25 sidecar could not be
rewritten at all once a search had mapped it - Windows refuses to truncate
or replace a mapped path, so a UI reindex died at the very end."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import core.store as store_mod
from core import vault as vault_mod
from core.config import IndexConfig
from core.embed.hashing import HashingEmbedder
from core.indexer import run_index
from core.search import search_page
from core.store import Store


def _corpus(tmp: str, count: int = 40) -> Path:
    root = Path(tmp) / "corpus"
    root.mkdir()
    for i in range(count):
        (root / f"f{i:02d}.txt").write_text(
            f"документ номер {i} про квартальный бюджет и план", encoding="utf-8")
    return root


class ProgressPhaseTests(unittest.TestCase):
    """Progress events must describe real phases, not race to 100%."""

    def test_scan_reports_progress_without_a_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _corpus(tmp)
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            events: list[tuple[int, int, str]] = []
            run_index([root], config, store, embedder,
                      progress=lambda pos, total, status, path:
                          events.append((pos, total, status)))
            store.close()
            scans = [event for event in events if event[2] == "scan"]
            self.assertTrue(scans, "the filesystem walk must report a scan phase")
            self.assertTrue(all(event[1] == 0 for event in scans),
                            "scan events carry no total, so no percent can be faked")
            worked = [event for event in events if event[2] != "scan"]
            self.assertTrue(worked, "file-level progress follows the scan")
            self.assertTrue(all(event[1] > 0 for event in worked),
                            "post-scan phases carry the real total")
            self.assertEqual(worked[-1][2], "finalize",
                             "the run ends with a finalize status, not a silent 100%")

    def test_second_run_reports_the_same_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _corpus(tmp)
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            run_index([root], config, store, embedder)
            events: list[tuple[int, int, str]] = []
            run_index([root], config, store, embedder,
                      progress=lambda pos, total, status, path:
                          events.append((pos, total, status)))
            store.close()
            scans = [event for event in events if event[2] == "scan"]
            self.assertTrue(scans, "a reindex scans again and says so")


class SidecarResealTests(unittest.TestCase):
    """The sidecar is a cache: a live mapped reader must not break either
    the reseal itself or the index run that triggers it."""

    def _sealed_index(self, tmp: str):
        root = Path(tmp) / "corpus"
        root.mkdir()
        (root / "a.txt").write_text("первый документ про квартальный бюджет", encoding="utf-8")
        (root / "b.txt").write_text("второй документ про план и отчёт", encoding="utf-8")
        config = IndexConfig(home=Path(tmp) / "home")
        embedder = HashingEmbedder()
        db = Path(tmp) / "index.db"
        store = Store(db, embedder.name, embedder.dim)
        run_index([root], config, store, embedder)
        vault_mod.enable(Path(tmp) / "home")
        store.seal_index()
        store.close()
        return root, config, embedder, db

    def test_reseal_replaces_a_mapped_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, embedder, db = self._sealed_index(tmp)
            sidecar = Path(str(db) + ".bm25seal")
            self.assertTrue(sidecar.exists())
            # the running server reads the sidecar through a live mapping
            reader_store = Store(db, embedder.name, embedder.dim, readonly=True)
            hits = search_page(reader_store, embedder, "бюджет",
                               limit=5, usage=reader_store.usage_map())
            self.assertTrue(hits.hits)
            # new content changes the corpus; the index run must refresh the
            # sidecar instead of dying on the mapped-file rule
            (root / "new.txt").write_text(
                "совершенно новый документ про бюджет и план", encoding="utf-8")
            job_store = Store(db, embedder.name, embedder.dim)
            stats = run_index([root], config, job_store, embedder)
            job_store.close()
            self.assertFalse(any("bm25seal" in str(path) for path, _ in stats.errors),
                             f"the reseal must not fail the run: {stats.errors}")
            # a cold reader loads the refreshed sidecar and its fingerprint
            # matches the new corpus
            store_mod._BM25["index"] = None
            store_mod._BM25["key"] = None
            store_mod._BM25["fp"] = None
            store = Store(db, embedder.name, embedder.dim, readonly=True)
            loaded = None
            try:
                loaded = store._load_sealed_bm25(store._chunks_fingerprint())
                self.assertIsNotNone(loaded, "the refreshed sidecar must load")
                self.assertEqual(tuple(loaded.fingerprint), store._chunks_fingerprint())
                hits = search_page(store, embedder, "совершенно новый",
                                   limit=5, usage=store.usage_map())
                self.assertTrue(hits.hits, "the new document must be findable")
            finally:
                if loaded is not None:
                    loaded.close()
                # the search just re-installed a mapped reader into the cache
                cached = store_mod._BM25.get("index")
                if cached is not None and hasattr(cached, "close"):
                    cached.close()
                store_mod._BM25["index"] = None
                store.close()
                reader_store.close()
                vault_mod.install(None)

    def test_sidecar_failure_does_not_fail_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, config, embedder, db = self._sealed_index(tmp)
            (root / "new.txt").write_text(
                "ещё один новый документ про бюджет и план", encoding="utf-8")
            store = Store(db, embedder.name, embedder.dim)

            def broken_write(index, fp=None):
                raise OSError(22, "Invalid argument", str(db) + ".bm25seal")

            store._write_sealed_bm25 = broken_write
            try:
                stats = run_index([root], config, store, embedder)
            finally:
                vault_mod.install(None)
            store.close()
            self.assertTrue(any(path == "bm25seal" for path, _ in stats.errors),
                            "a locked sidecar is recorded, not fatal")


if __name__ == "__main__":
    unittest.main()
