"""Recent-file feed for the minimal menu: real indexed rows, newest first.

The menu needs files a person actually touched, ordered by last open with the
index timestamp as the fallback. The feed is read-only and bounded.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import api
from core.embed import HashingEmbedder
from core.store import Store


class RecentFeedTests(unittest.TestCase):
    def test_recent_uses_last_open_then_index_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            embedder = HashingEmbedder()
            store = Store(db, embedder.name, embedder.dim)
            try:
                store.conn.execute("INSERT INTO dirs(dir_id, path) VALUES(1, 'C:\\corpus')")
                rows = [
                    (1, "old.py", 10.0, 10.0, 0),
                    (2, "opened.js", 5.0, 5.0, 900.0),
                    (3, "new.json", 20.0, 20.0, 0),
                ]
                for file_id, name, indexed, mtime, last_open in rows:
                    store.conn.execute(
                        "INSERT INTO file_rows(id, dir_id, name, size, mtime, sha256, kind, indexed_at) "
                        "VALUES(?, 1, ?, 12, ?, x'01', 'code', ?)",
                        (file_id, name, mtime, indexed),
                    )
                    store.conn.execute(
                        "INSERT INTO usage(file_id, opens, last_open) VALUES(?, 1, ?)",
                        (file_id, last_open),
                    )
                store.commit()
            finally:
                store.close()

            handler = object.__new__(api.Handler)
            captured: dict = {}
            handler._json = lambda payload: captured.update(payload)
            handler._error = lambda message, status=400: captured.update(error=message)

            def open_store():
                return Store(db, embedder.name, embedder.dim)

            with patch.object(api.Handler, "_store", side_effect=open_store):
                api.Handler._recent(handler, {"limit": ["3"]})

            self.assertNotIn("error", captured)
            recent = captured.get("recent", [])
            self.assertEqual([item["name"] for item in recent], ["opened.js", "new.json", "old.py"])
            self.assertEqual(recent[0]["file_id"], 2)
            self.assertEqual(recent[0]["ext"], "js")
            self.assertIn("path", recent[0])


if __name__ == "__main__":
    unittest.main()
