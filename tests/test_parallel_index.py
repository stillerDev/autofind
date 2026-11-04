"""Parallel extraction: the worker path must land exactly what the
sequential path lands, in a store that searches identically."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.config import IndexConfig
from core.embed import get_embedder
from core.indexer import run_index
from core.query import understand
from core.search import search_page
from core.store import Store

FILES = 14


def make_corpus(root: Path) -> Path:
    docs = root / "docs"
    docs.mkdir(parents=True)
    for index in range(FILES):
        (docs / f"doc_{index:02d}.txt").write_text(
            f"Document {index} about {['databases', 'networking', 'rendering'][index % 3]}. "
            + "Filler sentence with enough bytes to clear the minimum. " * 6,
            encoding="utf-8",
        )
    return docs


class ParallelIndexTests(unittest.TestCase):
    def _index(self, tmp: str, name: str, workers: int | None):
        config = IndexConfig()
        config.home = Path(tmp) / f"home-{name}"
        config.home.mkdir(parents=True)
        docs = make_corpus(Path(tmp) / name)
        embedder = get_embedder("auto", config.model_dir)
        store = Store(Path(tmp) / f"{name}.db", embedder.name, embedder.dim)
        try:
            stats = run_index([docs], config, store, embedder, workers=workers)
            page = search_page(store, embedder, "databases", limit=FILES, offset=0, sort=None,
                               parsed=understand("databases"), usage={}, applied=False)
            return stats, page
        finally:
            store.close()

    def test_parallel_lands_the_same_index_as_sequential(self):
        with tempfile.TemporaryDirectory() as tmp:
            sequential, seq_page = self._index(tmp, "seq", workers=1)
            parallel, par_page = self._index(tmp, "par", workers=2)
            self.assertEqual(sequential.indexed, FILES)
            self.assertEqual(parallel.indexed, FILES)
            self.assertEqual(parallel.failed, 0)
            self.assertEqual(sequential.chunks, parallel.chunks)
            # Equal scores are tie-broken by path, the two corpora live in
            # different folders ("seq" vs "par"), and parallel extraction can
            # change the last decimal of a score. The ranked set is the part
            # that must match.
            def ranked(page):
                return sorted(Path(hit.path).name for hit in page.hits)

            self.assertEqual(ranked(seq_page), ranked(par_page))

    def test_explicit_one_worker_is_the_sequential_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats, _ = self._index(tmp, "single", workers=1)
            self.assertEqual(stats.indexed, FILES)
            self.assertEqual(stats.failed, 0)


if __name__ == "__main__":
    unittest.main()
