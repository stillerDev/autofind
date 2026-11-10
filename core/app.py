"""The desktop app: a real window instead of a browser tab.

Same HTML, same server, one difference that matters — the UI lives in its own
window with its own icon, closes to the tray instead of dying, and comes back
on a global hotkey while you are typing somewhere else. That is the difference
between "a folder with a local site in it" and "a program".

The server still runs in this process on loopback: one process, one index, one
cache. Two processes would mean two ports and two copies of the vector pack.

Nothing here is required by the CLI or by MCP. If pywebview is missing,
`available()` is False and the caller falls back to the browser.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from .config import mark_onboarding_done, onboarding_done

APP_TITLE = "AutoFind"
# The UI has three columns; below ~1280 the right one starts clipping its own
# buttons. 1360x860 is the smallest size where everything fits on a 1080p
# screen with the taskbar showing.
DEFAULT_SIZE = (1360, 860)
MIN_SIZE = (1000, 640)
# First run opens as a small, centred card rather than a full work area: the
# welcome screen is a conversation, and a conversation in a maximised window
# feels like a dialog box that swallowed the desktop. 820x820 is the smallest
# square that still holds the split setup screen without scrolling the greeting.
FIRST_RUN_SIZE = (820, 820)
FIRST_RUN_MIN = (700, 600)
# The split setup screen stops growing here: past ~1120 px the two columns turn
# into empty margins, and the window is supposed to move aside, not take over.
SPLIT_SIZE = (1120, 780)
DEFAULT_HOTKEY = "ctrl+alt+space"
GEOMETRY_FILE = "window.json"
LOCK_FILE = "app.lock"
PORT_FILE = "app.port"


def available() -> bool:
    """True when a native window can actually be created on this machine."""
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    if sys.platform == "win32":
        # pywebview needs pythonnet for the EdgeChromium backend; without it
        # it silently falls back to the old MSHTML engine, where this UI's
        # backdrop-filter and CSS variables do not render.
        try:
            import clr  # noqa: F401
        except Exception:
            return False
    return True


def icon_path() -> Path | None:
    root = Path(__file__).resolve().parent.parent
    for name in ("icon.ico", "icon.png"):
        candidate = root / "web" / name
        if candidate.exists():
            return candidate
    return None


# --------------------------------------------------------------- single instance


def acquire_instance_lock(home: Path):
    """Hold an exclusive lock for the life of the process, or return None.

    Two windows would mean two servers, two indexes and a tray icon you cannot
    tell apart. The lock is released by the OS when the process dies, so a
    crash never leaves the app permanently "already running".
    """
    home.mkdir(parents=True, exist_ok=True)
    handle = open(home / LOCK_FILE, "a+b")
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def release_instance_lock(handle) -> None:
    if handle is None:
        return
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass
    finally:
        try:
            handle.close()
        except OSError:
            pass


# ------------------------------------------------------------------- geometry


def load_geometry(home: Path) -> dict:
    try:
        data = json.loads((home / GEOMETRY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_geometry(home: Path, geometry: dict) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / GEOMETRY_FILE).write_text(json.dumps(geometry, indent=2), encoding="utf-8")
    except OSError:
        pass


SPI_GETWORKAREA = 0x0030


def work_area() -> tuple[int, int, int, int] | None:
    """The desktop minus the taskbar, in the units pywebview measures in.

    A truly fullscreen window covers the taskbar too, and the taskbar is where
    the clock, the tray icon and every other running program live. Borderless
    is what the eye wants; covering the screen is not.
    """
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETWORKAREA, 0, ctypes.byref(rect), 0
        )
        if not ok:
            return None
        width = int(rect.right) - int(rect.left)
        height = int(rect.bottom) - int(rect.top)
        if width <= 0 or height <= 0:
            return None
        return (int(rect.left), int(rect.top), width, height)
    except Exception:
        return None


def fit_work_area(title: str) -> None:
    """Snap an already-created window to the work area.

    Belt and braces: the size passed to `create_window` should already be right,
    but pywebview scales those numbers by the monitor DPI and a rounding step
    there is enough to push the bottom edge back under the taskbar.
    """
    area = work_area()
    if area is None:
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return
        left, top, width, height = area
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(
            hwnd, 0, left, top, width, height, SWP_NOZORDER | SWP_NOACTIVATE
        )
    except Exception:
        pass


def find_window(title: str = APP_TITLE, tries: int = 20) -> int | None:
    """The HWND behind pywebview's title, or None while it is still being built."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        for _ in range(tries):
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                return int(hwnd)
            time.sleep(0.1)
    except Exception:
        pass
    return None


def dpi_scale(hwnd: int | None) -> float:
    """Physical pixels per logical pixel for this window.

    pywebview creates windows in logical pixels and then multiplies by this
    factor, while `SetWindowPos` and `SystemParametersInfo` speak physical
    pixels. A 820x820 card on a 125% display is 1025x1025 physical, and a
    window that forgets this opens visibly smaller than requested.
    """
    if sys.platform != "win32" or not hwnd:
        return 1.0
    try:
        import ctypes

        dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd))
        return dpi / 96.0 if dpi > 0 else 1.0
    except Exception:
        return 1.0


def window_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (
            int(rect.left),
            int(rect.top),
            int(rect.right) - int(rect.left),
            int(rect.bottom) - int(rect.top),
        )
    except Exception:
        return None


def place_window(hwnd: int, rect: tuple[int, int, int, int]) -> None:
    """Move and resize without stealing focus from the webview."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        x, y, width, height = rect
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, int(x), int(y), int(width), int(height),
            SWP_NOZORDER | SWP_NOACTIVATE,
        )
    except Exception:
        pass


def center_window(hwnd: int, width: int, height: int) -> None:
    """Centre a logical size inside the work area, in physical pixels."""
    area = work_area()
    if area is None:
        return
    left, top, area_width, area_height = area
    scale = dpi_scale(hwnd)
    phys_width = min(int(width * scale), area_width)
    phys_height = min(int(height * scale), area_height)
    place_window(
        hwnd,
        (
            left + (area_width - phys_width) // 2,
            top + (area_height - phys_height) // 2,
            phys_width,
            phys_height,
        ),
    )


def window_geometry(window) -> dict:
    """Read back size and position, ignoring anything the GUI refuses to say."""
    out: dict = {}
    for key in ("width", "height", "x", "y"):
        try:
            out[key] = int(getattr(window, key))
        except Exception:
            pass
    try:
        out["maximized"] = window.state.maximized
    except Exception:
        pass
    return out


# ------------------------------------------------------------------ hotkeys


def pynput_hotkey(spec: str) -> str:
    """`ctrl+alt+space` -> `<ctrl>+<alt>+space`, the syntax pynput wants."""
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    return "+".join(f"<{part}>" if len(part) > 1 else part for part in parts)


# --------------------------------------------------------------------- icon


def set_app_user_model_id(app_id: str = "AutoFind.Search") -> None:
    """Give the process its own taskbar identity, so it groups with our icon."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def polish_window(title: str = APP_TITLE, ico: Path | None = None) -> None:
    """Dark title bar and the real icon, applied to the window we just opened.

    The WinForms host gives a stock light title bar above a near-black UI, which
    looks like a screenshot pasted into a form. DwmSetWindowAttribute(20) fixes
    that on Windows 10 2004+; attribute 19 is the older build. Both are best
    effort — a light title bar is ugly, not broken.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        import time

        user32 = ctypes.windll.user32
        hwnd = None
        for _ in range(20):          # the host window appears a moment later
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
            time.sleep(0.1)
        if not hwnd:
            return

        value = ctypes.c_int(1)
        for attribute in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value)
            ) == 0:
                break

        if ico is not None and Path(ico).exists():
            IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
            WM_SETICON = 0x0080
            handle = user32.LoadImageW(None, str(ico), IMAGE_ICON, 0, 0, LR_LOADFROMFILE)
            if handle:
                user32.SendMessageW(hwnd, WM_SETICON, 0, handle)   # small
                user32.SendMessageW(hwnd, WM_SETICON, 1, handle)   # big

        # Nudge the non-client area to repaint with the new colours.
        SWP = 0x0002 | 0x0001 | 0x0004 | 0x0010 | 0x0020
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP)
    except Exception:
        pass


# ------------------------------------------------------- second launch


def write_port(home: Path, port: int) -> None:
    try:
        home.mkdir(parents=True, exist_ok=True)
        (home / PORT_FILE).write_text(str(int(port)), encoding="utf-8")
    except OSError:
        pass


def read_port(home: Path) -> int | None:
    try:
        return int((home / PORT_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def focus_existing(home: Path) -> bool:
    """Ask the running app to come forward. True when it answered."""
    port = read_port(home)
    if not port:
        return False
    try:
        import urllib.request

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/focus", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


# --------------------------------------------------------------- window API


class _WindowApi:
    """What the page is allowed to ask the window to do.

    With no title bar the app's own header is the only place with a close
    button, so the page needs a way to reach the window. Deliberately tiny: the
    UI can hide or quit itself, nothing else. It exists only inside the app —
    in a browser `window.pywebview` is undefined and the buttons stay hidden.
    """

    def __init__(self, app: "_App") -> None:
        self._app = app

    def hide(self) -> bool:
        self._app.hide()
        return True

    def quit(self) -> bool:
        self._app.quit()
        return True

    def toggle(self) -> bool:
        self._app.toggle()
        return True

    # The first-run screen is two layouts in one window. The page knows when
    # to switch; only the host can resize the actual window, so it asks here.
    def onboarding_split(self) -> bool:
        self._app.onboarding_split()
        return True

    def onboarding_finish(self) -> bool:
        self._app.finish_onboarding()
        return True

    def onboarding_reopen(self) -> bool:
        self._app.reopen_onboarding()
        return True


# ------------------------------------------------------------------ the app


class _App:
    def __init__(self, url: str, home: Path, *, tray: bool, hotkey: str | None,
                 start_hidden: bool = False, debug: bool = False,
                 borderless: bool = True) -> None:
        self.url = url
        self.home = home
        self.want_tray = tray
        self.hotkey = hotkey
        self.start_hidden = start_hidden
        self.debug = debug
        self.borderless = borderless
        self.window = None
        self.tray = None
        self.listener = None
        self.quitting = False
        self.hidden = start_hidden
        # A hidden first launch (login entry) is not a welcome screen: nobody is
        # looking at it. The welcome is shown the first time a window is seen.
        self.first_run = not onboarding_done(home) and not start_hidden
        self._geometry = load_geometry(home)
        self._anim_token = 0
        self._anim_lock = threading.Lock()

    # -- window ------------------------------------------------------------

    def build(self):
        import webview

        geometry = self._geometry
        width = int(geometry.get("width") or DEFAULT_SIZE[0])
        height = int(geometry.get("height") or DEFAULT_SIZE[1])
        x = geometry.get("x")
        y = geometry.get("y")

        min_size = MIN_SIZE
        if self.first_run and self.borderless:
            # pywebview takes logical pixels; WinForms centres a window with no
            # explicit x/y itself, and `_on_loaded` then pins the exact work-area
            # centre in physical pixels once the DPI is known.
            width, height = FIRST_RUN_SIZE
            x = y = None
            min_size = FIRST_RUN_MIN

        # Borderless is the default: this is a search box, not a document
        # window, and the Windows title bar is four buttons the app already
        # replaces with its own. It stops at the work area instead of covering
        # the screen, so the taskbar stays visible and clickable — the tray icon
        # this app lives in is down there. `--windowed` brings the frame back.
        area = work_area() if self.borderless and not self.first_run else None
        if area is not None:
            x, y, width, height = area
        else:
            # Never open larger than the screen: a window whose bottom is under
            # the taskbar hides the very buttons this app is about.
            try:
                screen = webview.screens[0]
                width = min(width, int(screen.width * 0.94))
                height = min(height, int(screen.height * 0.90))
            except Exception:
                pass

        window = webview.create_window(
            APP_TITLE,
            self.url,
            width=width,
            height=height,
            x=x if isinstance(x, int) else None,
            y=y if isinstance(y, int) else None,
            min_size=min_size,
            maximized=bool(geometry.get("maximized")) and not self.start_hidden
            and not self.borderless,
            frameless=self.borderless,
            easy_drag=False,
            hidden=self.start_hidden,
            background_color="#101418",
            text_select=True,
            confirm_close=False,
            js_api=_WindowApi(self),
        )
        self.window = window
        window.events.closing += self._on_closing
        window.events.closed += self._on_closed
        window.events.loaded += self._on_loaded
        return window

    def _on_loaded(self) -> None:
        def polish() -> None:
            if self.borderless:
                if self.first_run:
                    hwnd = find_window()
                    if hwnd:
                        center_window(hwnd, *FIRST_RUN_SIZE)
                else:
                    fit_work_area(APP_TITLE)
            polish_window(APP_TITLE, icon_path())

        threading.Thread(target=polish, name="autofind-polish", daemon=True).start()

    def _on_closing(self) -> bool:
        # Closing the window means "get out of my way", not "stop searching":
        # the index and the server stay warm, which is the whole point of a
        # launcher. Quitting is an explicit action in the tray menu.
        if self.quitting:
            return True
        # But if neither the tray nor the hotkey came up, hiding would leave a
        # process with no way back in. Then closing really closes.
        if self.tray is None and self.listener is None:
            return True
        self.hide()
        return False

    def _on_closed(self) -> None:
        self.quitting = True

    # -- actions -----------------------------------------------------------

    def show(self) -> None:
        if self.window is None:
            return
        try:
            self.window.show()
            if not self.borderless:
                # restore() re-applies the size from the geometry file; a
                # borderless window is pinned to the work area and must not be
                # pulled back to an older, smaller rectangle.
                self.window.restore()
            self.window.on_top = True
            self.window.on_top = False
        except Exception:
            pass
        self.hidden = False

    def hide(self) -> None:
        if self.window is None:
            return
        try:
            self.window.hide()
            self.hidden = True
            self._trim_memory()
        except Exception:
            pass

    @staticmethod
    def _trim_memory() -> None:
        """Hand the working set back while hidden, the way kensaku does.

        Hiding the window means the index caches (vector pack, name index) are
        the only thing keeping hundreds of megabytes resident. EmptyWorkingSet
        does not free anything — it just lets Windows page the cold pages out,
        so the tray-idle footprint drops and the next show faults them back in
        at memory speed.
        """
        if sys.platform != "win32":
            return
        try:
            import ctypes

            handle = ctypes.windll.kernel32.GetCurrentProcess()
            ctypes.windll.psapi.EmptyWorkingSet(handle)
        except Exception:
            pass  # cosmetic; never worth breaking the hide over

    # -- first-run layout --------------------------------------------------

    def onboarding_split(self) -> None:
        """Grow the welcome card into two columns without leaving the centre.

        The card starts centred and the settings column unfolds beside the
        greeting, so the window only grows around its own middle: the greeting
        stays exactly where the eye left it. The move is done on the host
        window, not with CSS: a page can animate its own contents but it
        cannot resize the frame it lives in. One eased `SetWindowPos` per
        frame at 60 Hz reads as the window opening up, whereas a single jump
        is exactly the "it opened another window" feeling the setup screen is
        trying to avoid.
        """
        if not self.first_run or not self.borderless:
            return
        area = work_area()
        hwnd = find_window(tries=3)
        if area is None or not hwnd:
            return
        left, top, area_width, area_height = area
        scale = dpi_scale(hwnd)
        width = min(int(SPLIT_SIZE[0] * scale), int(area_width * 0.82))
        height = min(int(SPLIT_SIZE[1] * scale), int(area_height * 0.86))
        x = left + max(0, (area_width - width) // 2)
        y = top + max(0, (area_height - height) // 2)
        self._animate_window((x, y, width, height), duration=0.55)

    def finish_onboarding(self) -> None:
        """Mark the setup complete and grow back into the search window."""
        self.first_run = False
        mark_onboarding_done(self.home)
        if self.borderless:
            area = work_area()
            if area is not None:
                self._animate_window(area, duration=0.6)

    def reopen_onboarding(self) -> None:
        """Shrink back to the welcome card; the page reloads into it next."""
        self.first_run = True
        if not self.borderless:
            return
        hwnd = find_window(tries=3)
        if hwnd:
            center_window(hwnd, *FIRST_RUN_SIZE)

    def _animate_window(self, target: tuple[int, int, int, int], duration: float = 0.5) -> None:
        if sys.platform != "win32":
            return
        hwnd = find_window(tries=3)
        if not hwnd:
            return
        with self._anim_lock:
            self._anim_token += 1
            token = self._anim_token
        threading.Thread(
            target=self._run_animation,
            args=(hwnd, target, duration, token),
            name="autofind-window-anim",
            daemon=True,
        ).start()

    def _run_animation(self, hwnd: int, target, duration: float, token: int) -> None:
        start = window_rect(hwnd)
        if start is None:
            return
        steps = max(12, int(duration * 60))
        for step in range(1, steps + 1):
            with self._anim_lock:
                if token != self._anim_token:
                    return  # a newer layout superseded this one
            progress = step / steps
            eased = 1 - (1 - progress) ** 3
            rect = tuple(
                int(round(begin + (end - begin) * eased))
                for begin, end in zip(start, target)
            )
            place_window(hwnd, rect)  # type: ignore[arg-type]
            time.sleep(duration / steps)

    def toggle(self) -> None:
        if self.hidden:
            self.show()
        else:
            self.hide()

    def quit(self) -> None:
        self.quitting = True
        # A borderless window's size is the work area, not a preference: saving
        # it would make the next `--windowed` run open edge to edge.
        geometry = (
            window_geometry(self.window)
            if self.window is not None and not self.borderless else {}
        )
        if geometry:
            save_geometry(self.home, geometry)
        if self.listener is not None:
            try:
                self.listener.stop()
            except Exception:
                pass
        if self.tray is not None:
            try:
                self.tray.stop()
            except Exception:
                pass
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass

    # -- tray --------------------------------------------------------------

    def start_tray(self) -> None:
        try:
            import pystray
            from PIL import Image
        except Exception:
            return

        path = icon_path()
        if path is None:
            return
        try:
            image = Image.open(path).convert("RGBA")
            image.load()
        except Exception:
            return

        menu = pystray.Menu(
            pystray.MenuItem("Открыть", lambda *_: self.show(), default=True),
            pystray.MenuItem("Скрыть", lambda *_: self.hide()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", lambda *_: self.quit()),
        )
        self.tray = pystray.Icon("autofind", image, APP_TITLE, menu)
        threading.Thread(target=self._run_tray, name="autofind-tray", daemon=True).start()

    def _run_tray(self) -> None:
        try:
            self.tray.run()
        except Exception:
            # A missing tray is not a reason to refuse to run.
            self.tray = None

    # -- hotkey ------------------------------------------------------------

    def start_hotkey(self) -> None:
        if not self.hotkey:
            return
        try:
            from pynput import keyboard
        except Exception:
            return
        try:
            spec = pynput_hotkey(self.hotkey)
            self.listener = keyboard.GlobalHotKeys({spec: self.toggle})
            self.listener.start()
        except Exception:
            self.listener = None


def run_app(
    db_path: Path,
    config,
    port: int = 8765,
    hotkey: str | None = DEFAULT_HOTKEY,
    tray: bool = True,
    start_hidden: bool = False,
    borderless: bool = True,
    debug: bool = False,
) -> int:
    """Open the window and block until it is closed for good."""
    if not available():
        print("pywebview is not installed; falling back to the browser", file=sys.stderr)
        return 1

    import webview

    from .api import Handler, prewarm, start_server

    lock = acquire_instance_lock(config.home)
    if lock is None:
        # Already running. A second double-click should raise the window the
        # user cannot see, not print "already running" and exit — that reads as
        # a crash when the window happens to be hidden in the tray.
        if focus_existing(config.home):
            return 0
        print(f"{APP_TITLE} is already running", file=sys.stderr)
        return 0

    set_app_user_model_id()
    server, url = start_server(db_path, config, port=port)
    threading.Thread(
        target=server.serve_forever, name="autofind-http", daemon=True
    ).start()
    prewarm(db_path, config)
    write_port(config.home, server.server_address[1])

    app = _App(url, config.home, tray=tray or start_hidden, hotkey=hotkey,
               start_hidden=start_hidden, debug=debug, borderless=borderless)
    window = app.build()
    Handler.focus_hook = app.show
    if tray or start_hidden:
        app.start_tray()
    app.start_hotkey()

    icon = icon_path()
    start_args: dict = {
        "debug": debug,
        "private_mode": False,
        "storage_path": str(config.home / "webview"),
    }
    if icon is not None:
        start_args["icon"] = str(icon)
    try:
        webview.start(**start_args)
    except TypeError:
        # Older pywebview without `icon=`; the window still opens.
        start_args.pop("icon", None)
        webview.start(**start_args)
    finally:
        app.quitting = True
        Handler.focus_hook = None
        if not borderless:
            geometry = window_geometry(window)
            if geometry:
                save_geometry(config.home, geometry)
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        # The MCP handler holds a read-only Store for the whole session. On
        # Windows that handle blocks deleting or replacing the index, so the
        # window must let go of it on the way out.
        try:
            from .api import MCP

            if MCP is not None:
                MCP.close()
        except Exception:
            pass
        release_instance_lock(lock)
    return 0


def open_app(**kwargs) -> int:
    """`run.py` entry point: prefer the window, fall back to the browser."""
    from .api import serve
    from .config import IndexConfig

    config = kwargs.pop("config", None) or IndexConfig()
    config.ensure_home()
    db_path = kwargs.pop("db_path", None) or config.db_path

    if available():
        return run_app(db_path, config, **kwargs)

    serve(
        db_path, config,
        port=kwargs.get("port", 8765),
        open_browser=True,
    )
    return 0
