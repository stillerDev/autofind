"""Draw the app icon: a magnifier on the same near-black the UI uses.

    python tools/make_icon.py

Writes `web/icon.png` (512) and `web/icon.ico` (16-256, for the window, the
tray and the shortcut). The dot inside the lens is the same white as the
launcher mark in the header, so the icon and the app look like one thing.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 512
BACKGROUND = (18, 22, 26, 255)
RING = (231, 233, 235, 255)
DOT = (231, 233, 235, 255)


def draw(size: int = SIZE) -> Image.Image:
    scale = size / 512
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas = ImageDraw.Draw(image)

    def s(value: float) -> float:
        return value * scale

    canvas.rounded_rectangle(
        (s(8), s(8), s(504), s(504)), radius=s(104), fill=BACKGROUND,
        outline=(42, 47, 53, 255), width=max(1, int(s(4))),
    )
    # Lens ring, then the handle as a thick round-capped line. The handle has
    # to start inside the ring's outer edge (224 + 112/√2 ≈ 303 at 45°) or the
    # icon reads as a circle with a stick next to it.
    canvas.ellipse(
        (s(112), s(112), s(336), s(336)),
        outline=RING, width=max(2, int(s(38))),
    )
    canvas.line(
        [(s(300), s(300)), (s(402), s(402))], fill=RING, width=max(2, int(s(44))),
    )
    canvas.ellipse((s(210), s(210), s(238), s(238)), fill=DOT)
    return image


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    web = root / "web"
    web.mkdir(exist_ok=True)

    image = draw()
    image.save(web / "icon.png")
    # One .ico carrying every size Windows asks for: 16 in the tray, 32 in the
    # title bar, 256 in the task switcher.
    image.save(web / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(f"wrote {web / 'icon.png'} and {web / 'icon.ico'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
