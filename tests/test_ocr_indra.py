"""Unit tests for the Indra OCR port.

Most of these are direct ports of the Rust port's unit tests in
`kensaku-new/src-tauri/src/ocr_indra/*.rs` — same inputs, same expected pixel
values and strings — so a change that breaks parity with the native port fails
here first. The pack-dependent pieces (sessions, downloads) are tested with
fakes; `AUTOFIND_INDRA_PACK_DIR` points every pack check at a temp dir.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from core.ocr import indra
from core.ocr.indra import (
    Crop,
    LineBox,
    Vocab,
    compose_hangul,
    crop_line,
    decode_lines,
    estimate_skew,
    flip,
    gray_from_image,
    is_leader_line,
    is_rtl,
    pack_dir,
    pack_present,
    page_is_rtl,
    reading_order,
    resize,
    rotate,
    verify_pack,
    width_batches,
)
from PIL import Image


def _gray(w: int, h: int, data: list[int]) -> np.ndarray:
    return np.asarray(data, dtype=np.uint8).reshape(h, w)


class ResizeTests(unittest.TestCase):
    def test_area_averages_and_linear_interpolates(self):
        # Port of `area_resize_averages_and_linear_resize_interpolates`.
        g = _gray(4, 2, [0, 100, 200, 255, 0, 100, 200, 255])
        self.assertEqual(resize(g, 2, 1).ravel().tolist(), [50, 228])
        g = _gray(2, 1, [0, 200])
        # cv2 INTER_LINEAR of [0, 200] to width 4: [0, 50, 150, 200]
        self.assertEqual(
            resize(g, 4, 2).ravel().tolist(), [0, 50, 150, 200, 0, 50, 150, 200]
        )

    def test_same_size_is_a_copy(self):
        g = _gray(3, 2, [1, 2, 3, 4, 5, 6])
        out = resize(g, 3, 2)
        out[0, 0] = 9
        self.assertEqual(g[0, 0], 1, "same-size resize must not alias the input")

    def test_quarter_turns_match_numpy_rot90(self):
        # The Rust port hand-rolls rot90; numpy has it built in. This pins the
        # mapping: VERTICAL is ccw, every other turn is cw.
        g = _gray(3, 2, [1, 2, 3, 4, 5, 6])
        self.assertEqual(np.rot90(g, 1).tobytes(), _gray(2, 3, [3, 6, 2, 5, 1, 4]).tobytes())
        self.assertEqual(np.rot90(g, -1).tobytes(), _gray(2, 3, [4, 1, 5, 2, 6, 3]).tobytes())

    def test_gray_from_image_composites_transparency_over_white(self):
        image = Image.new("RGBA", (2, 1))
        image.putdata([(0, 0, 0, 0), (255, 0, 0, 255)])
        out = gray_from_image(image)
        self.assertEqual(out[0, 0], 255, "transparent pixel reads as paper")
        # 299/9601... of red 255 = 76 (the BT.601 weights shifted by 14 bits).
        self.assertEqual(out[0, 1], 76)


class DecodeLinesTests(unittest.TestCase):
    def test_detector_output_decodes_to_one_box_per_component(self):
        # Port of `detector_output_decodes_to_one_box_per_component`.
        h, w = 2, 6
        out = np.full((3, h, w), -10.0, dtype=np.float32)
        for i, j in [(0, 1), (0, 2), (1, 1), (1, 2)]:
            out[0, i, j] = 10.0
        out[0, 0, 5] = 10.0  # a lone pixel: dropped
        out[1, :, :] = 3.0
        out[2, :, :] = 3.0
        boxes = decode_lines(out, h, w)
        self.assertEqual(len(boxes), 1)
        b = boxes[0]
        # Rows 0 and 1 have centres 1 and 3: mean top 2 - 3 = -1, bottom 5.
        self.assertAlmostEqual(b.y0, -1.0, places=5)
        self.assertAlmostEqual(b.y1, 5.0, places=5)
        self.assertAlmostEqual(b.x0, 2.0 - 0.9, places=5)
        self.assertAlmostEqual(b.x1, 6.0 + 0.9, places=5)

    def test_out_of_bounds_returns_nothing(self):
        self.assertEqual(decode_lines(np.zeros((3, 2, 6), dtype=np.float32), 2, 6), [])


class CropTests(unittest.TestCase):
    def test_crop_keeps_text_scale_and_inverts_dark_lines(self):
        # Port of `crop_keeps_text_scale_and_inverts_dark_lines`.
        g = np.full((40, 100), 255, dtype=np.uint8)
        # Text-like: thin strokes on paper, not a solid block.
        for x in range(20, 80, 4):
            g[15:25, x] = 0
        box = LineBox(20.0, 15.0, 80.0, 25.0)
        self.assertFalse(indra.is_dark_line(g, box))
        c = crop_line(g, box, 32, 1, False)
        self.assertEqual(c.h, 32)
        # 66 x 14 px crop scaled to 32 high.
        self.assertEqual(c.w, 151)
        centre_row = c.ink[16]
        self.assertTrue((centre_row > 0.5).any(), "line centre has ink")
        self.assertTrue((c.ink[0] < 0.1).all(), "top margin is paper")
        tall = crop_line(g, box, 32, 3, False)
        self.assertEqual((tall.h, tall.w), (96, c.w))

        inverted = 255 - g
        self.assertTrue(indra.is_dark_line(inverted, box))

    def test_crop_of_an_off_page_box_pads_with_paper(self):
        g = np.full((20, 20), 255, dtype=np.uint8)
        c = crop_line(g, LineBox(0.0, 0.0, 10.0, 5.0), 8, 1, False)
        self.assertEqual(c.h, 8)
        self.assertEqual(c.ink.shape, (8, c.w))
        self.assertTrue((c.ink <= 1.0).all())


class BatchTests(unittest.TestCase):
    def test_batches_group_similar_widths(self):
        # Port of `batches_group_similar_widths`.
        self.assertEqual(width_batches([100, 300, 110, 120, 40]), [[4], [0, 2, 3], [1]])

    def test_pad_batch_right_pads_to_a_multiple_of_eight(self):
        out = indra.pad_batch(
            [Crop(h=1, w=3, ink=np.ones((1, 3), np.float32)),
             Crop(h=1, w=9, ink=np.ones((1, 9), np.float32))]
        )
        self.assertEqual(out.shape, (2, 1, 1, 16))
        self.assertEqual(int((out == 1.0).sum()), 12)


class SkewTests(unittest.TestCase):
    def test_rotation_by_zero_is_identity_and_quarter_turns_move_pixels(self):
        # Port of `rotation_by_zero_is_identity_and_quarter_turns_move_pixels`.
        g = np.full((5, 5), 255, dtype=np.uint8)
        g[2, 4] = 0  # right of centre
        self.assertEqual(rotate(g, 0.0, True).tobytes(), g.tobytes())
        # 90 degrees counter-clockwise: the right of centre goes up.
        r = rotate(g, 90.0, True)
        darkest = int(r.argmin())
        self.assertEqual((darkest % 5, darkest // 5), (2, 0))

    def test_a_tilted_page_is_measured_and_a_blank_page_is_left_alone(self):
        # Port of `a_tilted_page_is_measured_and_a_blank_page_is_left_alone`.
        page = np.full((300, 400), 255, dtype=np.uint8)
        for row in range(40, 260, 24):
            for x in range(40, 360):
                if x % 5 != 0:
                    page[row : row + 8, x] = 0
        tilted = rotate(page, 7.0, True)
        a = estimate_skew(tilted)
        self.assertLessEqual(abs(a + 7.0), 0.3, f"measured {a}")
        self.assertEqual(estimate_skew(np.full((100, 100), 255, dtype=np.uint8)), 0.0)


if __name__ == "__main__":
    unittest.main()


class VocabTests(unittest.TestCase):
    def _vocab(self, chars: list[str]) -> Vocab:
        # meta.json entries are JSON *strings* holding the dict.
        return Vocab(json.dumps({"blank": 0, "chars": chars}))

    def test_decodes_ids_and_spaces_latex_commands(self):
        # Port of `decodes_ids_and_spaces_latex_commands`.
        v = self._vocab(["a", "b", " ", "\\alpha", "x"])
        self.assertEqual(v.decode([1, 3, 2]), "a b")
        self.assertEqual(v.decode([4, 5]), "\\alpha x")
        self.assertEqual(v.decode([0, 99]), "", "blank and unknown ids are dropped")
        self.assertEqual(v.decode_chars([3, 1, 3, 3, 2, 3]), "a b")

    def test_rejects_a_nonzero_blank(self):
        with self.assertRaises(ValueError):
            Vocab({"blank": 1, "chars": []})
        with self.assertRaises(ValueError):
            Vocab({"blank": 0})

    def test_hangul_jamo_compose_into_syllables(self):
        # Port of `hangul_jamo_compose_into_syllables`.
        self.assertEqual(
            compose_hangul("한글"), "한글"
        )
        self.assertEqual(compose_hangul("abc"), "abc")


class RtlTests(unittest.TestCase):
    def test_rtl_flip_matches_the_reference_and_is_its_own_inverse(self):
        # Port of `rtl_flip_matches_the_reference_and_is_its_own_inverse`.
        # Visual "םולש" (as the recognizer reads it) is logical "שלום".
        self.assertEqual(flip("םולש", True), "שלום")
        # Latin and digits keep their order inside a right-to-left line.
        self.assertEqual(flip("2024 םולש", True), "שלום 2024")
        # Left-to-right base: only the Hebrew run reverses.
        self.assertEqual(flip("see םולש now", False), "see שלום now")
        for text, base in [("(בא) abc 12", True), ("x אב y", False)]:
            self.assertEqual(flip(flip(text, base), base), text)

    def test_page_rtl_needs_a_majority_of_letters(self):
        self.assertTrue(page_is_rtl(["שלום", "abc"]))
        self.assertFalse(page_is_rtl(["hello", "שלום"]))

    def test_marks_follow_their_base_when_reversed(self):
        # Arabic with a diacritic: the mark is part of the base's cluster, so
        # the reversed run keeps it after its letter.
        self.assertEqual(flip("سَا", False), "اسَ")
        # And the operation is its own inverse on this text too.
        self.assertEqual(flip(flip("سَا", False), False), "سَا")

    def test_is_rtl_excludes_digits_and_marks(self):
        self.assertTrue(is_rtl("א"))
        self.assertTrue(is_rtl("س"))
        self.assertFalse(is_rtl("١"), "Arabic-Indic digits read LTR")
        self.assertFalse(is_rtl("\u064e"), "a mark is not a letter")


class OrderTests(unittest.TestCase):
    def test_two_columns_read_column_by_column(self):
        # Port of `two_columns_read_column_by_column`.
        def line(x0: float, y0: float) -> LineBox:
            return LineBox(x0, y0, x0 + 80.0, y0 + 10.0)

        boxes = [line(0.0, 0.0), line(120.0, 0.0), line(0.0, 15.0), line(120.0, 15.0)]
        self.assertEqual(reading_order(boxes), [0, 2, 1, 3])


class LeaderTests(unittest.TestCase):
    def test_leader_lines_are_dropped_but_lone_dashes_stay(self):
        # Port of `leader_lines_are_dropped_but_lone_dashes_stay`.
        self.assertTrue(is_leader_line("......", 50.0, 10.0))
        self.assertTrue(is_leader_line(".", 5.0, 10.0))
        self.assertFalse(is_leader_line("-", 5.0, 10.0))
        self.assertFalse(is_leader_line("a.b", 50.0, 10.0))


class PackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_env = os.environ.get("AUTOFIND_INDRA_PACK_DIR")
        os.environ["AUTOFIND_INDRA_PACK_DIR"] = str(Path(self.tmp.name) / "ocr-indra")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("AUTOFIND_INDRA_PACK_DIR", None)
        else:
            os.environ["AUTOFIND_INDRA_PACK_DIR"] = self._old_env

    def test_pack_dir_follows_the_env_override(self):
        self.assertEqual(pack_dir(), Path(self.tmp.name) / "ocr-indra")

    def test_pack_present_needs_every_file(self):
        self.assertFalse(pack_present())
        directory = pack_dir()
        directory.mkdir(parents=True)
        for name in indra.PACK_FILES[:-1]:
            (directory / name).write_bytes(b"")
        self.assertFalse(pack_present())
        (directory / "meta.json").write_bytes(b"")
        self.assertTrue(pack_present())

    def test_verify_pack_reports_missing_and_bad_checksums(self):
        directory = pack_dir()
        directory.mkdir(parents=True)
        self.assertTrue(verify_pack().startswith("missing det.onnx"))
        artifact = indra.INDRA_ARTIFACTS[0]
        (directory / artifact.name).write_bytes(b"x" * 16)
        problem = verify_pack()
        self.assertIn("expected", problem)
        pad = artifact.bytes_expected % 256
        (directory / artifact.name).write_bytes(
            bytes(range(256)) * (artifact.bytes_expected // 256) + bytes(range(pad))
        )
        problem = verify_pack()
        self.assertIn("checksum", problem)

    def test_status_explains_a_missing_pack(self):
        status = indra.indra_status()
        self.assertFalse(status["available"])
        self.assertIn("download", status["problem"])


# --------------------------------------------------------------------- engine


class FakeSession:
    """Stands in for an onnxruntime session: canned outputs in name order."""

    def __init__(self, names: list[str], outputs: list[np.ndarray]) -> None:
        self.names = names
        self.outputs = outputs
        self.feeds: list[dict] = []

    def get_outputs(self):
        class _Out:
            def __init__(self, name: str) -> None:
                self.name = name

        return [_Out(name) for name in self.names]

    def run(self, _out_names, feed):
        self.feeds.append(feed)
        return self.outputs


def _vocab_json(chars: list[str]) -> str:
    return json.dumps({"blank": 0, "chars": chars})


def _det_output(h: int, w: int, cores: list[tuple[int, int, int, int]]) -> np.ndarray:
    """Detector logits: one core rectangle per line, fixed distances."""
    out = np.full((3, h, w), -10.0, dtype=np.float32)
    for y0, y1, x0, x1 in cores:
        out[0, y0:y1, x0:x1] = 10.0
    out[1, :, :] = 3.0
    out[2, :, :] = 3.0
    return out


def _rec_output(texts: list[str], vocab_chars: list[str]) -> list[np.ndarray]:
    """ids/conf/lengths tensors that decode to `texts` with one token per char."""
    index = {char: position + 1 for position, char in enumerate(vocab_chars)}
    seqs = []
    for text in texts:
        seq = [index[text[0]]]
        for prev, char in zip(text, text[1:]):
            if char == prev:  # CTC needs a blank between equal neighbours
                seq.append(0)
            seq.append(index[char])
        seqs.append(seq)
    steps = max((len(s) for s in seqs), default=0) or 1
    ids = np.zeros((len(texts), steps), dtype=np.int64)
    conf = np.full((len(texts), steps), 0.9, dtype=np.float32)
    lengths = np.asarray([len(s) for s in seqs], dtype=np.int64)
    for row, seq in enumerate(seqs):
        ids[row, : len(seq)] = seq
    return [ids, conf, lengths]


def _engine(cores, texts, vocab_chars, window=1, script=None, orient=None):
    det = FakeSession(["logits"], [_det_output(4, 32, cores)])
    rec = FakeSession(["ids", "conf", "lengths"], _rec_output(texts, vocab_chars))
    return indra.Engine(
        det=det,
        rec=rec,
        vocab=Vocab(_vocab_json(vocab_chars)),
        groups=[],
        script_session=script,
        orient=orient,
        long_side=64,
        height=32,
        window=window,
        script_groups=["base", "cjk", "rtl", "other"],
    )


class EngineTests(unittest.TestCase):
    def test_reads_one_line_end_to_end(self):
        # A page of paper with one dark line: the detector sees it, the
        # recognizer reads "hello", and `read()` returns exactly that.
        page = np.full((16, 32), 255, dtype=np.uint8)
        page[8, 4:20] = 0  # one ink row: the detector core
        engine = _engine([(0, 1, 2, 6)], ["hello"], list("helo"))
        self.assertEqual(engine.read(page), "hello")

    def test_blank_page_reads_as_empty(self):
        page = np.full((16, 32), 255, dtype=np.uint8)
        engine = _engine([], [], list("helo"))
        self.assertEqual(engine.read(page), "")
        self.assertEqual(engine.rec.feeds, [], "an empty page never reaches the recognizers")

    def test_leader_lines_are_dropped_from_the_output(self):
        # A dot-leader line is detected and read, but carries no text, so the
        # page reads as empty.
        page = np.full((16, 32), 255, dtype=np.uint8)
        page[8, 4:20] = 0
        engine = _engine([(0, 1, 2, 6)], ["...."], list("."))
        self.assertEqual(engine.read(page), "")

    def test_low_confidence_lines_are_silenced(self):
        # MIN_CONF exists because rules and empty fields read as "-", "I", "1".
        page = np.full((16, 32), 255, dtype=np.uint8)
        page[8, 4:20] = 0
        engine = _engine([(0, 1, 2, 6)], ["h"], list("h"))
        det = engine.det
        rec = engine.rec
        rec.outputs = [
            np.asarray([[1]], dtype=np.int64),
            np.asarray([[0.3]], dtype=np.float32),  # below MIN_CONF
            np.asarray([[1]], dtype=np.int64),
        ]
        self.assertEqual(engine.read(page), "")

    def test_two_columns_read_left_column_first(self):
        # Two ink rows, one per column, same top: reading order sorts by the
        # XY-cut, so the left line comes back first.
        page = np.full((16, 64), 255, dtype=np.uint8)
        page[8, 4:24] = 0
        page[8, 40:60] = 0
        engine = _engine(
            [(0, 1, 2, 6), (0, 1, 20, 26)], ["aa", "bb"], list("ab")
        )
        self.assertEqual(engine.read(page), "aa\nbb")

    def test_vertical_pages_route_everything_to_cjk(self):
        # A vertical page (orient class 4) sends all lines to the cjk group,
        # and cjk vocab reads whitespace-collapsed.
        page = np.full((16, 32), 255, dtype=np.uint8)
        page[8, 4:20] = 0
        cjk_vocab = Vocab(_vocab_json(["a", "b"]))
        rec = FakeSession(["ids", "conf", "lengths"], _rec_output(["ba"], ["a", "b"]))
        group_session = FakeSession(["ids", "conf", "lengths"], _rec_output(["ab"], ["a", "b"]))
        orient = FakeSession(["p"], [np.asarray([[0.01, 0.01, 0.01, 0.01, 0.99]], np.float32)])
        engine = indra.Engine(
            det=FakeSession(["logits"], [_det_output(4, 8, [(0, 1, 2, 6)])]),
            rec=rec,
            vocab=Vocab(_vocab_json(["x", "y"])),
            groups=[indra.Group("cjk", group_session, cjk_vocab)],
            script_session=None,
            orient=orient,
            long_side=64,
            height=32,
            window=1,
            script_groups=["base", "cjk", "rtl", "other"],
        )
        self.assertEqual(engine.read(page), "ab")
        # The base recognizer was never asked: the vertical gate skips it.
        self.assertEqual(rec.feeds, [])

    def test_orientation_of_a_sure_top_upright_page_does_not_turn(self):
        # orient probabilities far below ORIENT_MIN: no quarter turn.
        page = np.full((16, 32), 255, dtype=np.uint8)
        page[8, 4:20] = 0
        orient = FakeSession(["p"], [np.asarray([[0.5, 0.5, 0.5, 0.5, 0.0]], np.float32)])
        engine = _engine([(0, 1, 2, 6)], ["hello"], list("helo"), orient=orient)
        self.assertEqual(engine.read(page), "hello")

    def test_script_classifier_routes_sure_lines_away_from_base(self):
        # The classifier says "cjk" with 0.99: base is skipped for that line
        # and the cjk reading wins.
        page = np.full((16, 32), 255, dtype=np.uint8)
        page[8, 4:20] = 0
        base_vocab = Vocab(_vocab_json(["z"]))
        cjk_vocab = Vocab(_vocab_json(["c"]))
        rec = FakeSession(["ids", "conf", "lengths"], _rec_output(["zzz"], ["z"]))
        group = indra.Group(
            "cjk",
            FakeSession(["ids", "conf", "lengths"], _rec_output(["ccc"], ["c"])),
            cjk_vocab,
        )
        script = FakeSession(
            ["probs"], [np.asarray([[0.005, 0.99, 0.003, 0.002]], np.float32)]
        )
        engine = indra.Engine(
            det=FakeSession(["logits"], [_det_output(4, 8, [(0, 1, 2, 6)])]),
            rec=rec,
            vocab=base_vocab,
            groups=[group],
            script_session=script,
            orient=None,
            long_side=64,
            height=32,
            window=1,
            script_groups=["base", "cjk", "rtl", "other"],
        )
        self.assertEqual(engine.read(page), "ccc")


if __name__ == "__main__":
    unittest.main()

