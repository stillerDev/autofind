"""The audit leftovers a user can feel: sliders that do something, units that
read correctly, an export Excel cannot turn into formulas, a progress bar that
knows it failed. Each check pins a contract between app.js and style.css."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"
APP = (WEB / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "style.css").read_text(encoding="utf-8")
HTML = (WEB / "index.html").read_text(encoding="utf-8")


class LookSliderTests(unittest.TestCase):
    def test_every_glass_blur_follows_the_slider(self):
        # One --blur variable owns all of them; a hardcoded blur(Npx) in a
        # backdrop-filter would ignore the Appearance slider.
        hardcoded = re.findall(
            r"backdrop-filter:\s*blur\(\d+px", CSS)
        self.assertEqual(hardcoded, [], "hardcoded blur bypasses --blur")
        self.assertGreaterEqual(CSS.count("var(--blur"), 14)

    def test_zero_blur_is_sharp_not_broken(self):
        # blur(0px) is a valid, sharp render; the slider's floor must be 0.
        self.assertIn("blur: clamp(saved.blur, 0, 24", APP)

    def test_card_alpha_reaches_the_card_surface(self):
        self.assertIn("var(--card-alpha", CSS)

    def test_panel_alpha_derives_a_glass_color(self):
        self.assertIn("--panel-glass", CSS)
        self.assertIn("var(--panel-alpha", CSS)
        self.assertIn("background: var(--panel-glass)", CSS)

    def test_wall_dim_shades_the_photo(self):
        self.assertIn("rgba(0, 0, 0, var(--wall-dim", CSS)


class OutputHygieneTests(unittest.TestCase):
    def test_csv_export_neutralises_formula_leading_cells(self):
        self.assertIn("/^[=+\\-@]/", APP)

    def test_size_label_has_mb_and_gb_tiers(self):
        body = re.search(r"function sizeLabel\(.*?\n\}", APP, re.S).group(0)
        self.assertIn("1073741824", body, "no GB tier")
        self.assertIn("1048576", body, "no MB tier")

    def test_failed_job_does_not_paint_the_bar_green(self):
        body = re.search(r"api\(\"/api/progress\"\)\.then.*?\}\);", APP, re.S).group(0)
        self.assertIn("job.error", body)
        self.assertIn("--red", body)

    def test_preview_id_must_be_a_positive_integer(self):
        self.assertIn("Number.isInteger(Number(initialPreview))", APP)

    def test_highlight_never_matches_the_entities_it_emits(self):
        body = re.search(r"function highlight\(.*?\n\}", APP, re.S).group(0)
        self.assertIn("new Set", body, "terms must be deduped")
        self.assertIn("mark|amp|lt|gt|quot", body, "entity words must be skipped")


class BootStageTests(unittest.TestCase):
    def test_boot_stage_is_translated(self):
        self.assertIn('id="boot-stage" data-i18n="boot.starting"', HTML)


class OpenApiContractTests(unittest.TestCase):
    def test_documented_parameters_match_the_handlers(self):
        src = Path(__file__).resolve().parents[1].joinpath("core", "openapi.py").read_text(encoding="utf-8")
        # the drift the audit caught: documents said `path`, handlers read `id`
        self.assertNotIn('"path", "pinned"', src)
        self.assertNotIn('["name", "limit"]', src)
        self.assertIn('["id", "chars"]', src)


if __name__ == "__main__":
    unittest.main()
