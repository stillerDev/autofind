"""Every text-bearing extension should be indexed; audio/video never are."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.extract import extract_text
from core.scanner import kind_for


class ExtensionCoverageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unknown_extension_with_text_is_indexed(self):
        for name in ("notes.zzz", "config.foobar", "LICENSE", "data.xyzq"):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text("alpha beta gamma\nsecond line\n", encoding="utf-8")
                self.assertEqual(kind_for(path), "text")
                self.assertIn("alpha beta", extract_text(path, kind_for(path)))

    def test_audio_and_video_are_excluded(self):
        for name in ("song.mp3", "clip.wav", "movie.mp4", "voice.flac"):
            with self.subTest(name=name):
                path = self.root / name
                path.write_text("RIFF WAVE fmt ASCII metadata\n", encoding="utf-8")
                self.assertEqual(kind_for(path), "other")
                self.assertEqual(extract_text(path, kind_for(path)), "")

    def test_known_kinds_keep_their_extractors(self):
        code = self.root / "main.rs"
        code.write_text("fn main() {}", encoding="utf-8")
        self.assertEqual(kind_for(code), "code")
        image = self.root / "shot.png"
        image.write_bytes(b"not really a png")
        self.assertEqual(kind_for(image), "image")


if __name__ == "__main__":
    unittest.main()
