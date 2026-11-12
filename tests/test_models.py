"""Model pack downloader: pinned artifacts, verified installs, mirrors.

The network is faked (a local file server behind a patched urlopen), so the
tests exercise the real download/verify/rename path — including checksum
failure and mirror fallback — without touching Hugging Face.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import models


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patched_pack(artifacts):
    """A PACKS["e5"] entry built around the given artifacts — PACKS["e5"] is a
    frozen dataclass holding the real pinned tuple, so tests swap the dict
    entry, not the module constant."""
    return mock.patch.dict(models.PACKS, {"e5": models.Pack("e5", "e5-small", artifacts, "test")})


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int) -> bytes:
        chunk, self._data = self._data[:size], self._data[size:]
        return chunk


def _patched_urls(data: bytes, fail_first: bool = False):
    """A urlopen that serves `data`; the first mirror errors when asked."""

    def opener(url, timeout=None):
        if fail_first and "huggingface.co" in url:
            raise OSError("network unreachable")
        return _FakeResponse(data)

    return opener


class StatusTests(unittest.TestCase):
    def test_status_reports_missing_pack(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"AUTOFIND_MODELS_DIR": tmp}):
                status = models.pack_status("e5")
        self.assertFalse(status["available"])
        self.assertIn("missing model.onnx", status["problem"])
        self.assertTrue(Path(status["pack_dir"]).name == "e5-small")

    def test_verify_detects_corrupted_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict("os.environ", {"AUTOFIND_MODELS_DIR": tmp}):
                directory = models.pack_dir("e5")
                directory.mkdir(parents=True)
                artifact = models.Artifact("tokenizer.json", "tokenizer.json", 1000, _sha(b"ok" * 500))
                with _patched_pack((artifact,)):
                    path = directory / artifact.name
                    path.write_bytes(b"corrupted " * 100)
                    problem = models.verify_pack("e5")
        self.assertIn("checksum mismatch", problem, "the corrupted content is caught")

    def test_unknown_pack_is_rejected(self):
        with self.assertRaises(KeyError):
            models.pack_dir("nope")


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(
            mock.patch.dict("os.environ", {"AUTOFIND_MODELS_DIR": self._tmp.name}).start
        )

    def test_download_verifies_and_installs_every_file(self):
        payloads = {
            artifact.remote: (bytes(range(256)) * (artifact.bytes_expected // 256 + 1))[
                : artifact.bytes_expected
            ]
            for artifact in models.E5_ARTIFACTS
        }
        pinned = tuple(
            models.Artifact(
                a.remote, a.name, a.bytes_expected, _sha(payloads[a.remote])
            )
            for a in models.E5_ARTIFACTS
        )

        def opener(url, timeout=None):
            for remote, data in payloads.items():
                if remote in url:
                    return _FakeResponse(data)
            raise AssertionError(f"unexpected url {url}")

        with _patched_pack(pinned), mock.patch.object(models.urllib.request, "urlopen", opener):
            directory = models.download_pack("e5", progress=lambda *a: None)
            models.download_pack("e5")  # second run: resume, no re-download
        for artifact in pinned:
            path = directory / artifact.name
            self.assertTrue(path.is_file())
            self.assertEqual(path.stat().st_size, artifact.bytes_expected)
            self.assertEqual(_sha(path.read_bytes()), artifact.sha256)
        self.assertFalse(list(directory.glob("*.part")), "no staging left behind")

    def test_checksum_mismatch_never_installs_the_file(self):
        artifact = models.Artifact("tokenizer.json", "tokenizer.json", 17_082_730, _sha(b"real"))
        with _patched_pack((artifact,)), mock.patch.object(
            models.urllib.request, "urlopen", _patched_urls(b"tampered")
        ):
            with self.assertRaises(RuntimeError) as caught:
                models.download_pack("e5")
        self.assertIn("checksum mismatch", str(caught.exception))
        directory = models.pack_dir("e5")
        self.assertFalse((directory / artifact.name).exists())
        self.assertFalse(list(directory.glob("*.part")))

    def test_first_mirror_down_falls_back_to_the_second(self):
        data = bytes(range(256)) * 4096

        def opener(url, timeout=None):
            if "huggingface.co" in url:
                raise OSError("network unreachable")
            return _FakeResponse(data)

        artifact = models.Artifact("tokenizer.json", "tokenizer.json", len(data), _sha(data))
        with _patched_pack((artifact,)), mock.patch.object(
            models.urllib.request, "urlopen", opener
        ):
            directory = models.download_pack("e5")
        self.assertEqual((directory / artifact.name).read_bytes(), data)


if __name__ == "__main__":
    unittest.main()
