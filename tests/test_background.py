"""The wallpaper route: what it accepts, what it refuses, where it lands.

The feature is two thirds interface and one third file handling, so the tests
here go over the real socket rather than calling the handler's private methods:
a data URL that only works because the test built it in memory would not prove
that the app can take one from a browser.
"""

from __future__ import annotations

import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from core import api
from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.store import Store

# A 1×1 PNG. Small enough to inline, real enough that `mimetypes` and the
# browser both recognise it.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def data_url(payload: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"


class BackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / "corpus"
        root.mkdir()
        (root / "notes.txt").write_text("hello\n", encoding="utf-8")
        cls.config = IndexConfig(home=Path(cls.tmp.name) / "home")
        cls.config.ensure_home()
        cls.db = Path(cls.tmp.name) / "index.db"
        embedder = HashingEmbedder()
        store = Store(cls.db, embedder.name, embedder.dim)
        run_index([root], cls.config, store, embedder)
        store.close()

        cls.server, cls.base = api.start_server(cls.db, cls.config, port=8790)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        # The MCP server opened the index and would keep it locked on Windows,
        # which makes the next test class fail while deleting its temp folder.
        if api.MCP is not None:
            api.MCP.close()
        cls.tmp.cleanup()

    def setUp(self):
        self.clear()

    def clear(self):
        self.post({"clear": True})

    def post(self, payload) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + "api/background",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def get(self) -> tuple[int, bytes, str]:
        try:
            with urllib.request.urlopen(self.base + "api/background", timeout=10) as response:
                return response.status, response.read(), response.headers["Content-Type"]
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers["Content-Type"]

    # ------------------------------------------------------------------ happy

    def test_an_uploaded_image_comes_back_byte_for_byte(self):
        status, body = self.post({"data": data_url(PNG_BYTES)})
        self.assertEqual(status, 200)
        self.assertEqual(body["bytes"], len(PNG_BYTES))

        status, raw, content_type = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(raw, PNG_BYTES)
        self.assertEqual(content_type, "image/png")

    def test_the_file_lives_beside_the_index_not_in_the_browser(self):
        self.post({"data": data_url(PNG_BYTES)})
        stored = self.config.home / "background.png"
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes(), PNG_BYTES)

    def test_a_new_extension_replaces_the_old_one(self):
        self.post({"data": data_url(PNG_BYTES)})
        self.assertTrue((self.config.home / "background.png").exists())

        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 64
        self.post({"data": data_url(jpeg, "image/jpeg")})

        self.assertFalse((self.config.home / "background.png").exists())
        self.assertTrue((self.config.home / "background.jpg").exists())
        status, raw, content_type = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(raw, jpeg)
        self.assertEqual(content_type, "image/jpeg")

    def test_no_temporary_file_is_left_behind(self):
        self.post({"data": data_url(PNG_BYTES)})
        leftovers = list(self.config.home.glob("background*.part"))
        self.assertEqual(leftovers, [])

    def test_clear_removes_the_file(self):
        self.post({"data": data_url(PNG_BYTES)})
        status, body = self.post({"clear": True})
        self.assertEqual(status, 200)
        self.assertTrue(body["cleared"])
        self.assertFalse((self.config.home / "background.png").exists())
        self.assertEqual(self.get()[0], 404)

    # ---------------------------------------------------------------- refusals

    def test_something_that_is_not_an_image_is_refused(self):
        status, body = self.post({"data": "https://example.com/cat.png"})
        self.assertEqual(status, 400)
        self.assertIn("data URL", body["error"])
        self.assertFalse((self.config.home / "background.png").exists())

    def test_an_unsupported_image_type_is_refused(self):
        status, body = self.post({"data": data_url(b"<svg/>", "image/svg+xml")})
        self.assertEqual(status, 400)
        self.assertIn("unsupported", body["error"])

    def test_an_oversized_image_is_refused_with_413(self):
        fat = b"\x89PNG\r\n\x1a\n" + b"\x00" * (api.BACKGROUND_MAX + 1)
        status, body = self.post({"data": data_url(fat)})
        self.assertEqual(status, 413)
        self.assertIn("MB", body["error"])
        # A refused upload must not touch what is already there.
        self.assertFalse((self.config.home / "background.png").exists())

    def test_an_empty_image_is_refused(self):
        status, body = self.post({"data": "data:image/png;base64,"})
        self.assertEqual(status, 400)

    def test_a_refused_upload_keeps_the_previous_wallpaper(self):
        self.post({"data": data_url(PNG_BYTES)})
        status, _ = self.post({"data": "data:image/png;base64,%%%%"})
        self.assertEqual(status, 400)
        status, raw, _ = self.get()
        self.assertEqual(status, 200)
        self.assertEqual(raw, PNG_BYTES)

    # -------------------------------------------------------------- interface

    def test_the_interface_offers_an_image_picker(self):
        page = urllib.request.urlopen(self.base, timeout=10).read().decode("utf-8")
        self.assertIn('id="wall-file"', page)
        self.assertIn('type="file"', page)
        script = urllib.request.urlopen(self.base + "app.js", timeout=10).read().decode("utf-8")
        self.assertIn("/api/background", script)


if __name__ == "__main__":
    unittest.main()
