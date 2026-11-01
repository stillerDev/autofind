"""Screenshot the appearance section in the real UI (manual visual check).

    python tests/shot_appearance.py http://127.0.0.1:8765

Not part of the test suite: it needs a running server and a display.
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8765"

    with sync_playwright() as play:
        browser = play.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base, wait_until="networkidle")
        page.wait_for_timeout(800)

        # Settings, then the Appearance block at the top of the drawer.
        page.click("#settings-btn")
        page.wait_for_selector("#settings.open", timeout=5000)
        page.wait_for_timeout(500)
        page.locator(".settings-card").screenshot(path="docs/ui-appearance.png")

        # A preset, so the picture shows the palette moving with the background.
        page.click('.wall-tile[data-wall="dusk"]')
        page.wait_for_timeout(600)
        page.locator(".settings-card").screenshot(path="docs/ui-appearance-dusk.png")
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        page.screenshot(path="docs/ui-dusk.png")

        # Back to the default so the docs are not a tour of a theme nobody has.
        page.click("#settings-btn")
        page.wait_for_timeout(400)
        page.click('.wall-tile[data-wall="forest"]')
        page.wait_for_timeout(500)
        print("wrote docs/ui-appearance.png, docs/ui-appearance-dusk.png, docs/ui-dusk.png")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
