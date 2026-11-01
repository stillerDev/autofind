"""Desktop app plumbing: window state, single instance, autostart, shortcuts.

No GUI is created here — that would open a window on every test run. What is
tested is everything around the window, because that is what breaks quietly:
a lock that does not lock, geometry that does not survive a restart, a login
entry that points at `python.exe` and flashes a console.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import app, autostart


class HotkeyTests(unittest.TestCase):
    def test_spec_is_translated_to_pynput_syntax(self):
        # pynput wraps named keys in angle brackets, including <space>.
        self.assertEqual(app.pynput_hotkey("ctrl+alt+space"), "<ctrl>+<alt>+<space>")
        self.assertEqual(app.pynput_hotkey("Ctrl + Shift + A"), "<ctrl>+<shift>+a")
        self.assertEqual(app.pynput_hotkey("f9"), "<f9>")

    def test_single_letter_keeps_the_bare_form(self):
        self.assertEqual(app.pynput_hotkey("ctrl+a"), "<ctrl>+a")

    def test_empty_spec_is_an_error(self):
        with self.assertRaises(ValueError):
            app.pynput_hotkey("  +  ")


class GeometryTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app.save_geometry(home, {"width": 1200, "height": 800, "x": 10, "y": 20})
            self.assertEqual(
                app.load_geometry(home),
                {"width": 1200, "height": 800, "x": 10, "y": 20},
            )

    def test_broken_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / app.GEOMETRY_FILE).write_text("{not json", encoding="utf-8")
            self.assertEqual(app.load_geometry(home), {})

    def test_non_dict_json_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / app.GEOMETRY_FILE).write_text("[1,2,3]", encoding="utf-8")
            self.assertEqual(app.load_geometry(home), {})


class InstanceLockTests(unittest.TestCase):
    def test_second_instance_is_refused_until_the_first_quits(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            first = app.acquire_instance_lock(home)
            self.assertIsNotNone(first)
            try:
                self.assertIsNone(app.acquire_instance_lock(home))
            finally:
                app.release_instance_lock(first)
            again = app.acquire_instance_lock(home)
            self.assertIsNotNone(again)
            app.release_instance_lock(again)


class PortTests(unittest.TestCase):
    def test_port_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app.write_port(home, 8765)
            self.assertEqual(app.read_port(home), 8765)

    def test_missing_or_broken_port_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertIsNone(app.read_port(home))
            (home / app.PORT_FILE).write_text("nonsense", encoding="utf-8")
            self.assertIsNone(app.read_port(home))

    def test_focus_without_a_port_file_is_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(app.focus_existing(Path(tmp)))


class FocusTests(unittest.TestCase):
    def test_focus_posts_to_the_running_app(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app.write_port(home, 8123)
            response = mock.MagicMock()
            response.status = 200
            response.__enter__ = lambda self: self
            response.__exit__ = lambda self, *args: False
            with mock.patch("urllib.request.urlopen", return_value=response) as open_url:
                self.assertTrue(app.focus_existing(home))
            request = open_url.call_args[0][0]
            self.assertEqual(request.full_url, "http://127.0.0.1:8123/api/focus")
            self.assertEqual(request.get_method(), "POST")

    def test_dead_app_is_reported_as_not_focused(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            app.write_port(home, 8123)
            with mock.patch("urllib.request.urlopen", side_effect=OSError("refused")):
                self.assertFalse(app.focus_existing(home))


class LauncherCommandTests(unittest.TestCase):
    def test_autostart_uses_pythonw_and_the_hidden_flag(self):
        target, arguments = autostart.launcher_target(hidden=True)
        self.assertTrue(target.lower().endswith(".exe"))
        if sys.platform == "win32":
            self.assertTrue(target.lower().endswith("pythonw.exe"), target)
        self.assertIn("run.py", arguments)
        self.assertIn("--hidden", arguments)

    def test_visible_launch_has_no_hidden_flag(self):
        _, arguments = autostart.launcher_target(hidden=False)
        self.assertNotIn("--hidden", arguments)

    def test_command_quotes_the_paths(self):
        command = autostart.launcher_command()
        self.assertTrue(command.startswith('"'))
        self.assertEqual(command.count('"'), 4)


class AutostartRegistryTests(unittest.TestCase):
    def test_status_without_the_value_is_off(self):
        with mock.patch.object(autostart, "supported", return_value=True), \
                mock.patch("winreg.OpenKey", side_effect=FileNotFoundError):
            state = autostart.status()
        self.assertFalse(state["enabled"])
        self.assertEqual(state["name"], autostart.VALUE_NAME)

    def test_status_reads_the_registered_command(self):
        with mock.patch.object(autostart, "supported", return_value=True), \
                mock.patch("winreg.OpenKey", return_value=mock.MagicMock()), \
                mock.patch("winreg.QueryValueEx", return_value=("cmd /c x", 1)):
            state = autostart.status()
        self.assertTrue(state["enabled"])
        self.assertEqual(state["registered"], "cmd /c x")

    def test_disable_reports_whether_something_was_removed(self):
        with mock.patch.object(autostart, "supported", return_value=True), \
                mock.patch("winreg.OpenKey", side_effect=FileNotFoundError):
            self.assertFalse(autostart.disable())

    def test_unsupported_platform_is_not_an_exception(self):
        with mock.patch.object(autostart, "supported", return_value=False):
            self.assertFalse(autostart.status()["supported"])
            self.assertFalse(autostart.disable())
            with self.assertRaises(RuntimeError):
                autostart.enable()


class ShortcutTargetTests(unittest.TestCase):
    def test_targets_are_inside_the_users_profile(self):
        from core import shortcut

        for path in shortcut.targets():
            self.assertTrue(str(path).lower().endswith("autofind.lnk"))
            # A per-user install must not write to the shared Public desktop.
            self.assertNotIn("public", str(path).lower())

    def test_icon_exists(self):
        icon = app.icon_path()
        self.assertIsNotNone(icon, "run tools/make_icon.py")
        self.assertTrue(icon.exists())


class WorkAreaTests(unittest.TestCase):
    """The borderless window is sized by the work area, so that is what is tested.

    A fullscreen window covers the taskbar; the taskbar is where the tray icon
    this app hides behind lives, so "as big as possible" and "still usable" are
    different rectangles.
    """

    def test_work_area_stops_at_the_taskbar(self):
        area = app.work_area()
        if area is None:
            self.skipTest("not win32")
        import ctypes

        left, top, width, height = area
        screen_w = ctypes.windll.user32.GetSystemMetrics(0)
        screen_h = ctypes.windll.user32.GetSystemMetrics(1)
        self.assertGreater(width, 0)
        self.assertGreater(height, 0)
        # Never larger than the screen, never so small that the layout clips.
        self.assertLessEqual(left + width, screen_w)
        self.assertLessEqual(top + height, screen_h)
        self.assertGreaterEqual(height, 600)

    def test_missing_window_is_not_an_exception(self):
        # Called on `loaded`; a window that has not appeared yet must not kill
        # the polish thread before the icon is set.
        self.assertIsNone(app.fit_work_area("AutoFind — no such window"))

    def test_borderless_is_the_default(self):
        self.assertTrue(app._App("http://x/", Path("."), tray=False, hotkey=None).borderless)

    def test_windowed_mode_asks_for_a_frame(self):
        instance = app._App("http://x/", Path("."), tray=False, hotkey=None, borderless=False)
        self.assertFalse(instance.borderless)


if __name__ == "__main__":
    unittest.main()
