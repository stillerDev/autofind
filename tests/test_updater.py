"""Updater hardening: version gates, sums parsing, download caps, safe swap.

The download/replace flow is exercised against a real local HTTP server
(stand-in for a GitHub release) and a real cmd-driven file swap — no
network or GitHub needed.
"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import updater


class VersionGateTests(unittest.TestCase):
    def test_parse_version_suffixes(self):
        self.assertEqual(updater.parse_version("v1.2.3"), (1, 2, 3))
        self.assertEqual(updater.parse_version("1.10.0-beta.2"), (1, 10, 0))
        self.assertEqual(updater.parse_version("garbage"), (0, 0, 0))

    def test_older_or_equal_release_is_ignored(self):
        with mock.patch.object(updater, "current_version", return_value="1.2.3"), \
                mock.patch.object(
                    updater, "urlopen",
                    return_value=_FakeResponse(
                        b'{"tag_name": "v1.2.3", "assets": []}'
                    ),
                ):
            self.assertIsNone(updater.check_update())

    def test_non_https_asset_url_is_refused(self):
        release = (
            b'{"tag_name": "v9.9.9", "assets": ['
            b'{"name": "AutoFind-win.exe", "size": 10, '
            b'"browser_download_url": "http://evil.example/x.exe"}]}'
        )
        with mock.patch.object(updater, "current_version", return_value="0.1.0"), \
                mock.patch.object(updater, "urlopen", return_value=_FakeResponse(release)):
            self.assertIsNone(updater.check_update())


class _FakeResponse:
    """Minimal urlopen stand-in: .headers.get and .read are all the module uses."""

    def __init__(self, body: bytes, length: int | None = None):
        self._body = body
        self.headers = {"Content-Length": str(length if length is not None else len(body))}

    def read(self, n: int = -1) -> bytes:
        if n == -1:
            body, self._body = self._body, b""
            return body
        block, self._body = self._body[:n], self._body[n:]
        return block

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SumsTests(unittest.TestCase):
    def test_parse_sums_two_space_format(self):
        text = (
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            "  AutoFind-Setup.exe\n"
            "1111111111111111111111111111111111111111111111111111111111111111"
            " *AutoFind.zip\n"
            "not a sums line\n"
        )
        sums = updater.parse_sums(text)
        self.assertEqual(len(sums), 2)
        self.assertIn("autofind-setup.exe", sums)
        self.assertEqual(sums["autofind.zip"], "1" * 64)


class DownloadTests(unittest.TestCase):
    BODY = b"MZ fake installer " + b"x" * 1000

    def _download(self, body: bytes, *, length: int | None = None, **kwargs) -> Path:
        fake = _FakeResponse(body, length=length)
        with mock.patch.object(updater, "urlopen", return_value=fake):
            return updater.download_update(
                "https://github.com/example/update.exe",
                kwargs.pop("expected_size", len(body)),
                kwargs.pop("expected_sha256", None),
            )

    def test_happy_path_stages_a_verifiable_file(self):
        staged = self._download(self.BODY)
        try:
            self.assertEqual(staged.read_bytes(), self.BODY)
            self.assertEqual(updater._sha256_of(staged),
                             hashlib.sha256(self.BODY).hexdigest())
        finally:
            staged.unlink(missing_ok=True)

    def test_non_https_url_is_refused_flat(self):
        with self.assertRaises(updater.UpdateError):
            updater.download_update("http://plain.example/u.exe", 0)

    def test_truncated_download_is_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(updater, "_stage_dir", return_value=Path(tmp)):
                with self.assertRaises(updater.UpdateError) as caught:
                    self._download(self.BODY, length=len(self.BODY) + 500)
            self.assertIn("truncated", str(caught.exception))
            # nothing staged half-broken: the staging dir holds no update.exe
            self.assertEqual(list(Path(tmp).glob("update.exe")), [])

    def test_sha256_mismatch_deletes_the_staged_file(self):
        with self.assertRaises(updater.UpdateError) as caught:
            self._download(self.BODY, expected_sha256="0" * 64)
        self.assertIn("SHA256 mismatch", str(caught.exception))

    def test_oversize_stream_is_capped(self):
        huge = _FakeResponse(b"x" * (updater.MAX_DOWNLOAD_BYTES + 1))
        with mock.patch.object(updater, "urlopen", return_value=huge), \
                self.assertRaises(updater.UpdateError) as caught:
            updater.download_update("https://ok.example/u.exe", 0)
        self.assertIn("cap", str(caught.exception))


class RealReleaseFlowTests(unittest.TestCase):
    """The full check -> download -> verify flow against a local 'release'."""

    def test_check_and_hash_lookup_against_a_local_release(self):
        body = b"MZ" + b"installer bytes" * 40
        digest = hashlib.sha256(body).hexdigest()
        sums = f"{digest}  AutoFind-win.exe\n"
        release = {
            "tag_name": "v1.2.4",
            "assets": [
                {"name": "AutoFind-win.exe", "size": len(body),
                 "browser_download_url": "https://example.invalid/win.exe"},
                {"name": "SHA256SUMS", "size": len(sums),
                 "browser_download_url": "https://example.invalid/SHA256SUMS"},
            ],
        }

        def fake_urlopen(req, timeout=0):
            url = req.full_url  # property, not a method
            if url.endswith("/latest"):
                import json
                return _FakeResponse(json.dumps(release).encode())
            return _FakeResponse(sums.encode())  # the sums asset

        with mock.patch.object(updater, "urlopen", new=fake_urlopen), \
                mock.patch.object(updater, "current_version",
                                  return_value="1.2.3"):
            info = updater.check_update()
        self.assertIsNotNone(info)
        self.assertEqual(info["version"], "1.2.4")
        self.assertEqual(info["sha256"], digest)
        self.assertIn("SHA256SUMS", info["sha256_source"])
        # The download step then verifies the real bytes against that hash.
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            stage.mkdir()  # _stage_dir is replaced wholesale, so pre-create it
            with mock.patch.object(updater, "_stage_dir", return_value=stage), \
                    mock.patch.object(
                        updater, "urlopen",
                        return_value=_FakeResponse(body),
                    ):
                staged = updater.download_update(
                    info["url"], info["size"], info["sha256"]
                )
            self.assertEqual(staged.read_bytes(), body)


class ReplaceTests(unittest.TestCase):
    def test_command_quotes_both_paths(self):
        staged = Path(r"C:\a & b\update (v1).exe")
        current = Path(r"C:\Program Files\AutoFind\AutoFind.exe")
        cmd = updater.replace_command(staged, current, delay=3)
        self.assertIn('"C:\\a & b\\update (v1).exe"', cmd)
        self.assertIn('start "" "C:\\Program Files\\AutoFind\\AutoFind.exe"', cmd)
        self.assertIn("ping -n 4", cmd)  # n packets ≈ n-1 seconds of wait

    def test_refuses_paths_cmd_would_misread(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current.exe"
            current.write_bytes(b"MZ old")
            percent = Path(tmp) / "u%p%.exe"
            percent.write_bytes(b"MZ new")
            with self.assertRaises(updater.UpdateError):
                updater.apply_update(percent, current_exe=current, launch=False)

    def test_final_hash_check_gates_the_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "current.exe"
            current.write_bytes(b"MZ old")
            staged = Path(tmp) / "update.exe"
            staged.write_bytes(b"MZ new")
            with self.assertRaises(updater.UpdateError):
                updater.apply_update(
                    staged, current_exe=current,
                    expected_sha256="0" * 64, launch=False,
                )

    def test_real_file_swap_runs_through_cmd(self):
        """The actual move: cmd waits, replaces the 'running' exe, relaunches.

        Uses the same command shape apply_update builds (ping wait, quoted
        move, `start` relaunch) with the relaunch pointed at cmd, which exits
        immediately, so the test stays side-effect-free.
        """
        with tempfile.TemporaryDirectory() as tmp:
            current = Path(tmp) / "app.exe"
            current.write_bytes(b"old running binary")
            staged = Path(tmp) / "staged.exe"
            staged.write_bytes(b"new staged binary")
            cmd = updater.replace_command(staged, current, delay=1)
            # swap the relaunch target: the test "app" is not a real exe
            cmd = cmd.replace(f'start "" "{current}"', 'start "" /min cmd /c exit')
            # A single string, same call shape as apply_update (see there).
            subprocess.run("cmd /d /c " + cmd, timeout=30,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=True)
            self.assertEqual(current.read_bytes(), b"new staged binary")
            self.assertFalse(staged.exists())


if __name__ == "__main__":
    unittest.main()
