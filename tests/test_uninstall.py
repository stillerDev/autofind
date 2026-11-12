"""The uninstaller: dry run leaves everything, --yes removes it all."""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from core.config import IndexConfig


class UninstallTests(unittest.TestCase):
    def _cli_args(self, yes: bool):
        import argparse

        return argparse.Namespace(command="uninstall", yes=yes)

    def test_dry_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config = IndexConfig(home=home)
            home.mkdir()
            (home / "index.db").write_bytes(b"x" * 100)
            (home / "vault.key").write_bytes(b"k" * 10)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                with mock.patch("core.autostart.disable") as dis, \
                        mock.patch("core.shortcut.remove") as rem:
                    from core.cli import _cmd_uninstall

                    code = _cmd_uninstall(self._cli_args(False), config)
            self.assertEqual(code, 0)
            self.assertIn("dry run", buffer.getvalue())
            self.assertIn("vault key=present", buffer.getvalue())
            # a dry run must not delete a thing
            self.assertTrue((home / "index.db").is_file())
            dis.assert_not_called()
            rem.assert_not_called()

    def test_yes_removes_the_data_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config = IndexConfig(home=home)
            home.mkdir()
            (home / "index.db").write_bytes(b"x" * 100)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                with mock.patch("core.autostart.disable", return_value=True), \
                        mock.patch("core.shortcut.remove", return_value=[]):
                    from core.cli import _cmd_uninstall

                    code = _cmd_uninstall(self._cli_args(True), config)
            self.assertEqual(code, 0)
            self.assertFalse(home.exists())

    def test_missing_home_is_a_clean_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = IndexConfig(home=Path(tmp) / "absent")
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                from core.cli import _cmd_uninstall

                code = _cmd_uninstall(self._cli_args(True), config)
            self.assertEqual(code, 0)
            self.assertIn("nothing to remove", buffer.getvalue())

    def test_failed_deletion_reports_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config = IndexConfig(home=home)
            home.mkdir()
            (home / "index.db").write_bytes(b"x" * 100)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                with mock.patch("core.autostart.disable", return_value=False), \
                        mock.patch("core.shortcut.remove", return_value=[]), \
                        mock.patch("shutil.rmtree", side_effect=OSError("locked")):
                    from core.cli import _cmd_uninstall

                    code = _cmd_uninstall(self._cli_args(True), config)
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
