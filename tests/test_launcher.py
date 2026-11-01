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

    def test_subcommands_reach_the_cli(self):
        for argv in (
            ["search", "смета"],
            ["index", "~/Downloads"],
            ["doctor"],
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
        ):
            self.assertFalse(run._is_cli(argv), argv)


class BareLaunchTests(unittest.TestCase):
    def test_bare_launch_starts_the_server_and_returns_zero(self):
        with mock.patch.object(run, "IndexConfig") as config_cls, \
                mock.patch("core.api.serve") as serve, \
                mock.patch("core.cli.main") as cli_main:
            config_cls.return_value.db_path = Path("index.db")
            self.assertEqual(run.main([]), 0)

        cli_main.assert_not_called()
        serve.assert_called_once()

    def test_launcher_does_not_open_a_browser_when_asked(self):
        with mock.patch.object(run, "IndexConfig") as config_cls, \
                mock.patch("core.api.serve") as serve:
            config_cls.return_value.db_path = Path("index.db")
            run.main(["--no-browser", "--port", "9123"])

        _, kwargs = serve.call_args
        self.assertFalse(kwargs["open_browser"])
        self.assertEqual(kwargs["port"], 9123)


if __name__ == "__main__":
    unittest.main()
