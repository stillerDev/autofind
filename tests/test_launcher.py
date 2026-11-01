"""The launcher must never fail on a bare invocation.

`python run.py` is what a double-click, a desktop shortcut and F5 in an editor
all do. It used to be routed to the CLI, whose parser then exited with
"the following arguments are required: command" — so the one gesture that has
to work above all others produced a traceback.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

import run


class RoutingTests(unittest.TestCase):
    def test_bare_invocation_is_the_launcher(self):
        self.assertFalse(run._is_cli([]))

    def test_every_launcher_flag_alone_reaches_the_launcher(self):
        """The bug this guards: a flag added to the parser but not to routing.

        `--hidden` was added to the parser and forgot in the flag list, so
        `run.py --hidden` fell through to the CLI and died on "the following
        arguments are required: command". Deriving the list from the parser
        makes that impossible; this test proves the derivation still holds.
        """
        for option in run.launcher_flags():
            self.assertFalse(run._is_cli([option]), option)

    def test_subcommands_reach_the_cli(self):
        for argv in (
            ["search", "смета"],
            ["index", "~/Downloads"],
            ["doctor"],
            ["app"],
            ["autostart", "on"],
            ["--db", "C:/x.db", "mcp"],     # the entry `connect` writes
            ["--vector-backend", "sqlite-vec", "search", "x"],
        ):
            self.assertTrue(run._is_cli(argv), argv)

    def test_launcher_flags_stay_with_the_launcher(self):
        for argv in (
            ["--port", "9000"],
            ["--port=9000"],
            ["--no-browser"],
            ["--db", "C:/x.db"],
            ["--db", "C:/x.db", "--port", "9000"],
            ["--hidden"],
            ["--browser"],
            ["--no-tray", "--no-hotkey"],
        ):
            self.assertFalse(run._is_cli(argv), argv)


class BareLaunchTests(unittest.TestCase):
    def test_bare_launch_opens_the_app_and_never_the_cli(self):
        with mock.patch.object(run, "IndexConfig") as config_cls, \
                mock.patch("core.app.available", return_value=True), \
                mock.patch("core.app.run_app", return_value=0) as run_app, \
                mock.patch("core.api.serve") as serve, \
                mock.patch("core.cli.main") as cli_main:
            config_cls.return_value.db_path = Path("index.db")
            self.assertEqual(run.main([]), 0)

        cli_main.assert_not_called()
        serve.assert_not_called()
        run_app.assert_called_once()

    def test_browser_flag_uses_the_browser(self):
        with mock.patch.object(run, "IndexConfig") as config_cls, \
                mock.patch("core.app.run_app") as run_app, \
                mock.patch("core.api.serve") as serve:
            config_cls.return_value.db_path = Path("index.db")
            self.assertEqual(run.main(["--browser"]), 0)

        run_app.assert_not_called()
        _, kwargs = serve.call_args
        self.assertTrue(kwargs["open_browser"])

    def test_no_browser_starts_the_server_only(self):
        with mock.patch.object(run, "IndexConfig") as config_cls, \
                mock.patch("core.api.serve") as serve:
            config_cls.return_value.db_path = Path("index.db")
            run.main(["--no-browser", "--port", "9123"])

        _, kwargs = serve.call_args
        self.assertFalse(kwargs["open_browser"])
        self.assertEqual(kwargs["port"], 9123)

    def test_hidden_flag_reaches_the_app(self):
        with mock.patch.object(run, "IndexConfig") as config_cls, \
                mock.patch("core.app.available", return_value=True), \
                mock.patch("core.app.run_app", return_value=0) as run_app:
            config_cls.return_value.db_path = Path("index.db")
            run.main(["--hidden"])

        _, kwargs = run_app.call_args
        self.assertTrue(kwargs["start_hidden"])


if __name__ == "__main__":
    unittest.main()
