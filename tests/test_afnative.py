"""Equivalence tests for the optional CPython hot-path extension.

The module is built by `python native/build_native.py`. It is optional, so the
tests skip cleanly on machines without a compiler or without the `.pyd`.
"""

from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

from core.extract.plain import extract_plain_bytes

try:
    _HAS_NATIVE = importlib.util.find_spec("afnative") is not None
except Exception:
    _HAS_NATIVE = False


@unittest.skipUnless(_HAS_NATIVE, "optional afnative extension is not built")
class NativeTextTests(unittest.TestCase):
    def _check(self, data: bytes, cap: int = 20 * 1024 * 1024) -> None:
        import afnative

        readers = [afnative.read_text_digest]
        win32 = getattr(afnative, "read_text_digest_win32", None)
        if win32 is not None:
            readers.append(win32)

        prefix = data[:cap]
        expected_digest = hashlib.sha256(prefix).digest()
        expected_text = extract_plain_bytes(prefix)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.txt"
            path.write_bytes(data)
            for read in readers:
                with self.subTest(reader=read.__name__):
                    text, digest = read(str(path), cap)
                    self.assertEqual(digest, expected_digest)
                    self.assertEqual(text or "", expected_text)

    def test_utf8(self):
        self._check("hello мир\n".encode("utf-8"))

    def test_utf8_bom(self):
        self._check(b"\xef\xbb\xbf" + "BOM текст".encode("utf-8"))

    def test_cp1251(self):
        self._check("привет мир".encode("cp1251"))

    def test_koi8_r(self):
        self._check("привет мир".encode("koi8-r"))

    def test_latin1(self):
        self._check("café naïve".encode("latin-1"))

    def test_binary(self):
        self._check(b"\x00\x01\x02binary")

    def test_control_heavy(self):
        self._check(bytes(range(1, 40)) * 200)

    def test_empty(self):
        self._check(b"")

    def test_large_text_uses_second_read(self):
        self._check(("line\n" * 400_000).encode("utf-8"))

    def test_cap_truncates_bytes(self):
        self._check(("abcdefghij" * 20).encode("utf-8"), cap=37)


if __name__ == "__main__":
    unittest.main()
