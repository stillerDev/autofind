"""The wallpaper and the palette derived from it.

The interesting part of this feature is not the file handling — that is covered
over the socket in `test_background.py`. It is the contract between three files
that never import each other: `index.html` paints the stored background before
the first frame, `app.js` owns the presets and the measurement, `style.css`
derives every surface from `--hue`. A rename in any one of them is silent in the
browser and obvious here.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from core import api
from tests.test_i18n import keys

WEB = Path(__file__).resolve().parents[1] / "web"
APP = WEB / "app.js"
PAGE = WEB / "index.html"
STYLE = WEB / "style.css"

PRESET = re.compile(
    r'\{\s*id:\s*"([\w-]+)",\s*wall:\s*(?:"([^"]*)"|\'([^\']*)\'),\s*hue:\s*(\d+),\s*sat:\s*(\d+)'
)


def presets() -> list[dict]:
    source = APP.read_text(encoding="utf-8")
    block = re.search(r"const WALLS = \[(.*?)\n\];", source, re.S)
    if block is None:
        raise AssertionError("no WALLS table in app.js")
    found = []
    for match in PRESET.finditer(block.group(1)):
        found.append(
            {
                "id": match.group(1),
                "wall": match.group(2) if match.group(2) is not None else match.group(3),
                "hue": int(match.group(4)),
                "sat": int(match.group(5)),
            }
        )
    return found


class PresetTests(unittest.TestCase):
    def test_there_are_presets_and_each_has_a_name_in_both_languages(self):
        table = presets()
        self.assertGreaterEqual(len(table), 3)
        english, russian = keys("en"), keys("ru")
        for preset in table:
            key = f"wall.{preset['id']}"
            self.assertIn(key, english)
            self.assertIn(key, russian)

    def test_preset_ids_are_unique(self):
        ids = [preset["id"] for preset in presets()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_preset_really_changes_the_hue(self):
        # A picker whose tiles all resolve to the same hue would look like it
        # works and repaint nothing.
        hues = {preset["hue"] for preset in presets()}
        self.assertEqual(len(hues), len(presets()))

    def test_the_first_preset_is_the_default_in_the_stylesheet(self):
        first = presets()[0]
        css = STYLE.read_text(encoding="utf-8")
        default = re.search(r"--wall:\s*([^;]+);", css).group(1).strip()
        self.assertEqual(default, first["wall"])
        self.assertIn(f"--hue: {first['hue']};", css)

    def test_a_preset_carries_a_wall_and_a_scheme_together(self):
        # The whole point of the feature: you pick a picture, not a hue.
        for preset in presets():
            self.assertTrue(preset["wall"].strip())
            self.assertGreaterEqual(preset["hue"], 0)
            self.assertLess(preset["hue"], 360)
            self.assertGreater(preset["sat"], 0)


class NoFlashTests(unittest.TestCase):
    """The stored background must be painted before the first frame."""

    def head(self) -> str:
        page = PAGE.read_text(encoding="utf-8")
        head = re.search(r"<head>(.*?)</head>", page, re.S)
        self.assertIsNotNone(head)
        return head.group(1)

    def test_the_script_is_in_the_head_and_not_deferred(self):
        head = self.head()
        self.assertIn("<script>", head)
        self.assertNotIn("defer", head)
        self.assertNotIn("src=", head)

    def test_it_reads_the_same_storage_key_the_app_writes(self):
        key = re.search(r'const WALL_KEY = "([^"]+)"', APP.read_text(encoding="utf-8")).group(1)
        self.assertIn(f'localStorage.getItem("{key}")', self.head())

    def test_it_sets_all_the_variables(self):
        head = self.head()
        for variable in ("--wall", "--hue", "--sat", "--wall-pos", "--wall-scale"):
            self.assertIn(f'"{variable}"', head)

    def test_a_blocked_storage_does_not_break_the_page(self):
        self.assertIn("catch", self.head())


class PaletteTests(unittest.TestCase):
    def test_every_surface_is_derived_from_the_hue(self):
        css = STYLE.read_text(encoding="utf-8")
        root = re.search(r":root \{(.*?)\}", css, re.S).group(1)
        for name in ("--bg", "--panel", "--panel-2", "--border", "--text", "--muted"):
            line = re.search(rf"{name}:\s*([^;]+);", root)
            self.assertIsNotNone(line, f"{name} is not declared")
            self.assertIn("hsl(var(--hue)", line.group(1), f"{name} ignores the hue")

    def test_the_wall_lives_on_its_own_layer_over_a_solid_fallback(self):
        css = STYLE.read_text(encoding="utf-8")
        # The photo is its own fixed layer so preset gradients and a chosen
        # picture can share the position/zoom controls without the image being
        # painted twice.
        rule = re.search(r"\n#wall \{(.*?)\}", css, re.S).group(1)
        self.assertIn("var(--wall)", rule)
        self.assertIn("--wall-pos", rule)
        self.assertIn("--wall-scale", rule)
        # A colour first, so a failed image load is a dark window and not white.
        body = re.search(r"\nbody \{(.*?)\}", css, re.S).group(1)
        self.assertRegex(body, r"background:\s*#[0-9a-f]{3,6}\s*;")

    def test_the_client_cap_does_not_exceed_the_server_cap(self):
        client = int(re.search(r"const WALL_MAX_MB = (\d+)", APP.read_text(encoding="utf-8")).group(1))
        self.assertLessEqual(client, api.BACKGROUND_MAX // 1048576)


class PickerTests(unittest.TestCase):
    def test_the_markup_has_one_tile_per_preset(self):
        # The tiles are static so their `data-i18n` labels exist at load; the
        # script only paints them. A preset added without a tile would be
        # invisible, and a tile without a preset would be a dead button.
        page = PAGE.read_text(encoding="utf-8")
        tiles = re.findall(r'data-wall="([\w-]+)"', page)
        self.assertEqual(sorted(tiles), sorted(preset["id"] for preset in presets()))

    def test_the_painting_comes_from_the_preset_table(self):
        source = APP.read_text(encoding="utf-8")
        body = re.search(r"function renderWalls\(\) \{(.*?)\n\}", source, re.S).group(1)
        self.assertIn("WALLS", body)
        self.assertIn("preset.wall", body)
        self.assertIn("aria-checked", body)

    def test_a_tile_sets_only_the_image_not_the_shorthand(self):
        # `tile.style.background = url(…)` resets background-size, so the 64×42
        # tile showed the top-left corner of a 1600×779 photo — a flat grey
        # rectangle that looked nothing like the wallpaper it was offering.
        body = re.search(
            r"function renderWalls\(\) \{(.*?)\n\}", APP.read_text(encoding="utf-8"), re.S
        ).group(1)
        self.assertIn("style.backgroundImage", body)
        self.assertNotIn("style.background =", body)

    def test_an_upload_goes_to_the_background_route(self):
        source = APP.read_text(encoding="utf-8")
        self.assertIn('api("/api/background"', source)
        self.assertIn("readAsDataUrl", source)

    def test_measuring_a_picture_averages_hues_not_channels(self):
        # A plain RGB average of a sunset is grey; the circular mean of the
        # saturated pixels is the colour you remember.
        source = APP.read_text(encoding="utf-8")
        body = re.search(r"function hueOfImage\((.*?)\n\}", source, re.S).group(1)
        self.assertIn("Math.sin", body)
        self.assertIn("Math.atan2", body)


class WallFitTests(unittest.TestCase):
    """The custom photo is framed in its own editor modal: a drag sets the free
    focus point (fx, fy) and a slider or the wheel sets the zoom. The controls
    are meaningless for the preset gradients, so the button that opens the
    editor is hidden unless the custom tile is the active one. These checks pin
    the contract between the markup, the names `app.js` writes, and the
    variables the stylesheet reads."""

    def test_the_editor_is_in_the_markup(self):
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn('id="wall-edit"', page)          # the button in settings
        self.assertIn('id="wall-editor"', page)        # the modal
        self.assertIn('id="wall-stage"', page)         # the drag surface
        self.assertIn('id="wall-canvas"', page)        # the preview layer
        self.assertIn('id="wall-zoom"', page)          # the slider
        self.assertIn('id="wall-editor-reset"', page)  # reset

    def test_the_editor_button_defaults_to_hidden(self):
        page = PAGE.read_text(encoding="utf-8")
        button = re.search(r'<button class="wall-edit"[^>]*>', page).group(0)
        self.assertIn("hidden", button)

    def test_the_app_writes_the_position_and_scale_variables(self):
        source = APP.read_text(encoding="utf-8")
        body = re.search(r"function applyTheme\(next\) \{(.*?)\n\}", source, re.S).group(1)
        self.assertIn("setProperty(\"--wall-pos\"", body)
        self.assertIn("setProperty(\"--wall-scale\"", body)

    def test_framing_is_only_for_the_custom_picture(self):
        source = APP.read_text(encoding="utf-8")
        edit = re.search(r"function renderWallEdit\(\) \{(.*?)\n\}", source, re.S).group(1)
        self.assertIn('edit.hidden = theme.id !== "custom"', edit)
        self.assertIn("openWallEditor", source)
        self.assertIn("closeWallEditor", source)

    def test_a_new_picture_starts_centred_and_unscaled(self):
        source = APP.read_text(encoding="utf-8")
        # useWallFile builds the custom theme; it must seed fx/fy/scale so the
        # editor has a defined state the first time it opens.
        line = re.search(r"const next = \{ id: \"custom\".*?\}\;", source).group(0)
        self.assertIn("fx: 0.5", line)
        self.assertIn("fy: 0.5", line)
        self.assertIn("scale: 1", line)

    def test_zoom_is_clamped_to_a_sane_range(self):
        source = APP.read_text(encoding="utf-8")
        # The minimum is 1: anything below lets the layer shrink away from the
        # screen edges and show the void behind it.
        self.assertIn("WALL_ZOOM_MIN = 1", source)
        self.assertIn("WALL_ZOOM_MAX = 2.5", source)
        body = re.search(r"function clampScale\(value\) \{(.*?)\n\}", source, re.S).group(1)
        self.assertIn("WALL_ZOOM_MIN", body)
        self.assertIn("WALL_ZOOM_MAX", body)


if __name__ == "__main__":
    unittest.main()
