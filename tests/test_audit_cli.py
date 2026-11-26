"""Regression tests for the audit fixes in cli/watch/updater/vault/doctor/app.

Only the files named in the audit scope are exercised here; shared tests are
left untouched.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from core import DEFAULT_PORT, __version__ as public_version
from core.__version__ import __version__
from core.config import IndexConfig
from core.embed.hashing import HashingEmbedder
from core.indexer import run_index
from core.personal import expand_aliases
from core.store import Store
from core.vault import Vault

ROOT = Path(__file__).resolve().parents[1]


def _config(tmp: str) -> IndexConfig:
    return IndexConfig(home=Path(tmp) / "home")


def _indexed(tmp: str) -> tuple[Path, Path]:
    corpus = Path(tmp) / "corpus"
    corpus.mkdir()
    target = corpus / "notes.txt"
    target.write_text("audit verify fixture: long enough to clear min_bytes", encoding="utf-8")
    db = Path(tmp) / "index.db"
    embedder = HashingEmbedder()
    store = Store(db, embedder.name, embedder.dim)
    try:
        run_index([corpus], _config(tmp), store, embedder)
    finally:
        store.close()
    return db, target


class UpdateFlowTests(unittest.TestCase):
    def test_cli_forwards_sha256_into_download_and_apply(self):
        import core.cli as cli

        info = {"version": "9.9.9", "url": "https://example.invalid/u.exe",
                "size": 123, "sha256": "a" * 64, "name": "u.exe"}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"AUTOFIND_HOME": tmp}), \
                mock.patch("core.updater.check_update", return_value=info), \
                mock.patch("core.updater.current_version", return_value="1.0.0"), \
                mock.patch("core.updater.download_update",
                           return_value=Path("staged.exe")) as download, \
                mock.patch("core.updater.apply_update") as apply:
            self.assertEqual(cli.main(["update"]), 0)
        download.assert_called_once_with(info["url"], 123, "a" * 64)
        apply.assert_called_once_with(Path("staged.exe"), expected_sha256="a" * 64)

    def test_declared_size_is_checked_even_with_matching_content_length(self):
        from core import updater

        class _Response:
            headers = {"Content-Length": "4"}

            def __init__(self):
                self._body = b"data"

            def read(self, n=-1):
                body, self._body = self._body, b""
                return body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(updater, "urlopen", return_value=_Response()), \
                mock.patch.object(updater, "_stage_dir",
                                  return_value=Path(tempfile.gettempdir())):
            with self.assertRaises(updater.UpdateError) as caught:
                updater.download_update("https://ok.example/u.exe", 999)
        self.assertIn("size mismatch", str(caught.exception))

    def test_apply_update_without_sys_frozen_attribute(self):
        from core import updater

        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "app.exe"
            staged = Path(tmp) / "update.exe"
            exe.write_bytes(b"current")
            staged.write_bytes(b"new")
            with mock.patch.object(sys, "executable", str(exe)):
                cmd = updater.apply_update(staged, current_exe=None, launch=False)
        self.assertIn("move /y", cmd)


class CliIntegrityTests(unittest.TestCase):
    def test_verify_detects_mtime_only_change(self):
        import core.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            db, target = _indexed(tmp)
            os.utime(target, (target.stat().st_atime + 100, target.stat().st_mtime + 100))
            buf = io.StringIO()
            with redirect_stdout(buf), mock.patch.dict(os.environ, {"AUTOFIND_HOME": tmp}):
                rc = cli._cmd_verify(argparse.Namespace(prune=False), _config(tmp), db)
            self.assertEqual(rc, 0)
            self.assertIn("1 changed", buf.getvalue())

    def test_watch_status_reads_recorded_roots(self):
        import core.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            db, _ = _indexed(tmp)
            store = Store(db, HashingEmbedder().name, HashingEmbedder().dim)
            try:
                store.set_meta(cli.ROOTS_META_KEY, json.dumps([r"C:\docs", r"D:\work"]))
                store.commit()
            finally:
                store.close()
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli._cmd_watch_status(argparse.Namespace(), _config(tmp), db)
            self.assertEqual(rc, 0)
            self.assertIn(r"root: C:\docs", buf.getvalue())
            self.assertIn(r"root: D:\work", buf.getvalue())

    def test_saved_and_alias_close_store_on_exception(self):
        import core.cli as cli

        for handler, kwargs in (
            (cli._cmd_saved, {"json": False, "drop": None, "query": None, "label": ""}),
            (cli._cmd_alias, {"json": False, "drop": None, "alias": None, "expand": None}),
        ):
            with self.subTest(handler=handler.__name__), tempfile.TemporaryDirectory() as tmp:
                db = Path(tmp) / "index.db"
                db.write_bytes(b"")
                with mock.patch.object(cli, "_open_store") as open_store, \
                        redirect_stdout(io.StringIO()), \
                        mock.patch("sys.stderr", io.StringIO()):
                    store = open_store.return_value
                    store.saved_searches.side_effect = RuntimeError("boom")
                    store.aliases.side_effect = RuntimeError("boom")
                    with self.assertRaises(RuntimeError):
                        handler(argparse.Namespace(**kwargs), _config(tmp), db)
                    store.close.assert_called_once()

    def test_seal_uses_configured_vector_backend(self):
        import core.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.db"
            db.write_bytes(b"")
            config = _config(tmp)
            config.vector_backend = "sqlite-vec"
            with mock.patch.object(cli, "get_embedder") as embedder, \
                    mock.patch("core.store.Store") as store_cls, \
                    mock.patch("core.vault.enable"):
                embedder.return_value.name = "hashing"
                embedder.return_value.dim = 384
                rc = cli._cmd_seal(argparse.Namespace(action="on"), config, db)
            self.assertEqual(rc, 0)
            self.assertEqual(store_cls.call_args.kwargs["vector_backend"], "sqlite-vec")

    def test_app_command_does_not_hardcode_hotkey(self):
        import core.cli as cli

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            config.ensure_home()
            args = argparse.Namespace(
                port=DEFAULT_PORT, browser=False, no_tray=False, no_hotkey=False,
                hotkey=None, hidden=False, windowed=False, debug=False,
            )
            with mock.patch("core.app.open_app", return_value=0) as opened, \
                    mock.patch.object(cli, "_shell_binary", return_value=None):
                rc = cli._cmd_app(args, config, Path(tmp) / "index.db")
            self.assertEqual(rc, 0)
            self.assertNotIn("hotkey", opened.call_args.kwargs)

    def test_ocr_read_forwards_cjk_settings(self):
        import core.cli as cli

        config = _config("")
        config.ocr_cjk = False
        config.ocr_cjk_model = "japan.onnx"
        config.ocr_cjk_keys = "japan.txt"
        with mock.patch("core.ocr.ocr_image") as ocr:
            ocr.return_value.engine = "stub"
            ocr.return_value.text = ""
            cli._cmd_ocr(argparse.Namespace(action="read", target="x.png",
                                            lang=None, json=False), config)
        kwargs = ocr.call_args.kwargs
        self.assertFalse(kwargs["cjk"])
        self.assertEqual(kwargs["cjk_model"], "japan.onnx")
        self.assertEqual(kwargs["cjk_keys"], "japan.txt")


class VaultCliTests(unittest.TestCase):
    def test_passphrase_vault_can_be_enabled_from_the_cli(self):
        import core.cli as cli
        from core import vault as vault_mod

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"AUTOFIND_HOME": tmp}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(
                cli.main(["vault", "passphrase", "--passphrase", "correct horse"]), 0
            )
            self.assertEqual(vault_mod.key_mode(Path(tmp)), "passphrase")
            self.assertEqual(cli.main(["vault", "status", "--json"]), 0)


class WatchTests(unittest.TestCase):
    def test_remote_root_refusal_clears_running(self):
        from core.watch import Watcher

        with tempfile.TemporaryDirectory() as tmp:
            watcher = Watcher([r"\\srv\share"], _config(tmp), Path(tmp) / "index.db")
            watcher._drive_type = lambda p: "remote"
            watcher.state.running = True
            watcher.run()
            self.assertFalse(watcher.state.running)
            self.assertIn("network drives", watcher.state.last_error or "")

    def test_event_path_join_uses_path_separator(self):
        from core.watch import EventFan

        fan = EventFan.__new__(EventFan)
        fan._lock = __import__("threading").Lock()
        fan._events = set()
        fan._push("C:/docs", "file.txt")
        self.assertIn(str(Path("C:/docs") / "file.txt"), fan._events)


class CryptoAndPersonalTests(unittest.TestCase):
    def test_arbitrary_afs1_prefix_is_resealed(self):
        vault = Vault(b"k" * 32)
        payload = b"AFS1" + b"plain text that merely starts with the magic"
        blob = vault.seal(payload)
        self.assertNotEqual(blob, payload)
        self.assertTrue(Vault.is_sealed(blob))
        self.assertEqual(vault.open_bytes(blob), payload)
        self.assertEqual(vault.seal(blob), blob)  # genuine idempotent re-seal

    def test_path_like_alias_is_expanded_literally(self):
        store = mock.Mock()
        store.aliases.return_value = {"cv": r"C:\work\cv"}
        out, applied = expand_aliases(store, "cv")
        self.assertEqual(out, r"C:\work\cv")
        self.assertEqual(applied[0].alias, "cv")

    def test_group_reference_alias_is_expanded_literally(self):
        store = mock.Mock()
        store.aliases.return_value = {"g": r"part\1end"}
        out, _ = expand_aliases(store, "g")
        self.assertEqual(out, r"part\1end")


class DoctorAndVersionTests(unittest.TestCase):
    def test_bundle_budget_counts_only_bundled_models(self):
        from core.doctor import estimate_bundle

        with tempfile.TemporaryDirectory() as tmp:
            bundle = estimate_bundle(_config(tmp))
        self.assertTrue(bundle["fits"])
        self.assertGreater(bundle["download_mb"], 0)
        self.assertTrue(any(p["part"].startswith("model:") for p in bundle["parts"]))

    def test_dir_size_survives_an_unreadable_file(self):
        from core.doctor import _dir_size

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("12345", encoding="utf-8")
            with mock.patch.object(Path, "stat", side_effect=OSError("denied")):
                self.assertEqual(_dir_size(root), 0)
            self.assertEqual(_dir_size(root), 5)

    def test_one_version_literal(self):
        import core.doctor as doctor

        self.assertEqual(public_version, __version__)
        self.assertEqual(doctor.__version__, __version__)
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('version = {attr = "core.__version__.__version__"}', pyproject)
        self.assertNotIn('version = "0.1.0"', pyproject)

    def test_packaging_manifests_declare_their_tools(self):
        bat = (ROOT / "AutoFind.bat").read_text(encoding="utf-8")
        self.assertIn("AUTOFIND_HOME", bat)
        self.assertIn("%HOME%\\shell.pid", bat)
        spec = (ROOT / "AutoFind-debug.spec").read_text(encoding="utf-8")
        self.assertIn("collect_data_files('rapidocr')", spec)
        self.assertNotIn("collect_data_files('rapidocr_onnxruntime')", spec)
        reqs = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        self.assertIn("pywin32>=306", reqs)


class AppTests(unittest.TestCase):
    def test_hidden_first_run_is_recalculated_on_show(self):
        import core.app as app

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(app, "onboarding_done", return_value=False):
            instance = app._App("http://x/", Path(tmp), tray=False,
                                hotkey=None, start_hidden=True)
            self.assertFalse(instance.first_run)
            instance.window = mock.Mock()
            instance.show()
            self.assertTrue(instance.first_run)

    def test_instance_lock_released_when_setup_fails(self):
        import core.app as app

        with tempfile.TemporaryDirectory() as tmp:
            config = _config(tmp)
            with mock.patch.dict(sys.modules, {"webview": mock.MagicMock()}), \
                    mock.patch.object(app, "available", return_value=True), \
                    mock.patch.object(app, "acquire_instance_lock",
                                      return_value=object()), \
                    mock.patch.object(app, "release_instance_lock") as release, \
                    mock.patch.object(app, "set_app_user_model_id"), \
                    mock.patch("core.api.start_server", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    app.run_app(Path(tmp) / "index.db", config)
            release.assert_called_once()

    def test_launcher_does_not_hardcode_the_hotkey(self):
        import run

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(run, "IndexConfig") as config_cls, \
                mock.patch("core.app.available", return_value=True), \
                mock.patch("core.app.run_app", return_value=0) as run_app:
            config_cls.return_value.db_path = Path(tmp) / "index.db"
            # Leave `home` as a mock: a real Path would take run.py's native
            # shell branch and spawn the resident WebView2 binary instead.
            run.main([])
            self.assertNotIn("hotkey", run_app.call_args.kwargs)
            run.main(["--no-hotkey"])
            self.assertIsNone(run_app.call_args.kwargs["hotkey"])

    def test_default_port_is_shared(self):
        import run

        self.assertEqual(DEFAULT_PORT, 8765)
        self.assertEqual(run.launcher_parser().parse_args([]).port, DEFAULT_PORT)


if __name__ == "__main__":
    unittest.main()
