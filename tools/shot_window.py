"""Capture the native window to a PNG (visual check, not part of the suite).

    python tools/shot_window.py "AutoFind" docs/shot-app.png

Renders the window itself with PrintWindow(PW_RENDERFULLCONTENT) instead of
copying the screen: the window does not have to be on top, and nothing else on
the desktop can end up in the screenshot. Falls back to a screen grab of the
window rectangle when the render comes back blank.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

from PIL import Image, ImageGrab, ImageStat

PW_RENDERFULLCONTENT = 0x00000002
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


def find(title: str) -> int:
    handle = ctypes.windll.user32.FindWindowW(None, title)
    if not handle:
        raise SystemExit(f"window not found: {title!r}")
    return handle


def render(hwnd: int) -> Image.Image:
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if width <= 0 or height <= 0:
        raise SystemExit("window has no size")

    screen = user32.GetDC(0)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    gdi32.SelectObject(memory, bitmap)
    user32.PrintWindow(hwnd, memory, PW_RENDERFULLCONTENT)

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height          # top-down
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    buffer = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS)

    image = Image.frombuffer("RGB", (width, height), buffer, "raw", "BGRX", 0, 1)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memory)
    user32.ReleaseDC(0, screen)
    return image


def grab_screen(hwnd: int) -> Image.Image:
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)                 # SW_RESTORE
    user32.BringWindowToTop(hwnd)
    rect = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return ImageGrab.grab(
        bbox=(rect.left, rect.top, rect.right, rect.bottom), all_screens=True
    )


def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else "AutoFind"
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/shot-app.png")
    hwnd = find(title)

    image = render(hwnd)
    # A window that refuses to render (some GPU surfaces) comes back black.
    if max(ImageStat.Stat(image.convert("L")).mean) < 1.5:
        image = grab_screen(hwnd)

    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)
    print(f"saved {out}  {image.width}x{image.height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
