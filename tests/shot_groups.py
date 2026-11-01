"""Screenshot the group badge in the real UI (manual visual check).

    python tests/shot_groups.py http://127.0.0.1:8823 "загрузка"

Not part of the test suite: it needs a running server and a display.
"""

from __future__ import annotations

import sys

from playwright.sync_api import sync_playwright


def main() -> int:
    base = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8823"
    query = sys.argv[2] if len(sys.argv) > 2 else "загрузка"

    with sync_playwright() as play:
        browser = play.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base, wait_until="networkidle")
        page.fill("input[type=search], #q", query)
        page.keyboard.press("Enter")
        page.wait_for_selector(".badge.b-group", timeout=15000)
        page.wait_for_timeout(600)
        page.screenshot(path="docs/shot-groups.png")

        badge = page.locator(".badge.b-group").first
        badge.click()
        page.wait_for_selector(".group-list", timeout=5000)
        page.wait_for_timeout(400)
        page.screenshot(path="docs/shot-groups-expanded.png")

        # A copy group is the case where the expanded rows must say *where*.
        copy = page.locator(".badge.b-group", has_text="копи").first
        if copy.count():
            copy.click()
            page.wait_for_timeout(600)
            page.screenshot(path="docs/shot-groups-copy.png")
            print("wrote docs/shot-groups.png, docs/shot-groups-expanded.png, docs/shot-groups-copy.png")
        else:
            print("wrote docs/shot-groups.png, docs/shot-groups-expanded.png (no copy group on this page)")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
