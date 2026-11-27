"""OCR box lookup must use the file id the UI sends, not the images row id.

The preview and result cards carry ``file_id`` from /api/search. An images row
has its own primary key, so querying ``images.id`` with a file id silently
returns no boxes and the highlight overlay never appears.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import api
from core.embed import HashingEmbedder
from core.store import Store


class OcrBoxesLookupTests(unittest.TestCase):
    def test_boxes_are_found_by_file_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            embedder = HashingEmbedder()
            store = Store(db, embedder.name, embedder.dim)
            try:
                columns = {row[1] for row in store.conn.execute("PRAGMA table_info(images)")}
                if "ocr_boxes" not in columns:
                    store.conn.execute("ALTER TABLE images ADD COLUMN ocr_boxes TEXT")
                boxes = [{"x": 4, "y": 8, "w": 30, "h": 12, "text": "HELLO"}]
                # Deliberately different ids: images.id is 7, the search-facing
                # file id is 2901. The UI can only ever send 2901.
                store.conn.execute(
                    "INSERT INTO images(id, path, file_id, width, height, ocr_boxes) "
                    "VALUES(7, ?, 2901, 100, 200, ?)",
                    ("C:\\shots\\a.png", json.dumps(boxes)),
                )
                store.commit()
            finally:
                store.close()

            handler = object.__new__(api.Handler)
            captured: dict = {}
            handler._json = lambda payload: captured.update(payload)
            handler._error = lambda message, status=400: captured.update(
                error=message, status=status)

            def open_store():
                return Store(db, embedder.name, embedder.dim)

            with patch.object(api.Handler, "_store", side_effect=open_store):
                api.Handler._ocr_boxes(handler, {"id": ["2901"], "q": ["hello"]})

            self.assertEqual(captured.get("error"), None, captured)
            self.assertEqual(captured.get("width"), 100, captured)
            self.assertEqual(captured.get("height"), 200, captured)
            self.assertEqual(len(captured.get("boxes", [])), 1, captured)
            self.assertEqual(captured["boxes"][0]["text"], "HELLO")


if __name__ == "__main__":
    unittest.main()
