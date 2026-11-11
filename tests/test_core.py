"""Unit tests. Runs on stdlib unittest: python -m unittest discover -s tests -t ."""

from __future__ import annotations

import gzip
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.chunker import chunk_text
from core.config import IndexConfig, save_privacy_prefs
from core.custom_types import coerce_types, find_type, normalise_mask, read_types, save_types
from core.doctor import estimate_bundle
from core.embed import HashingEmbedder
from core.embed.visual import LAYOUT_DIM, LayoutEmbedder
from core.evaluate import evaluate, matches
from core.extract import extract_text
from core.extract.archive import extract_archive
from core.extract.email import extract_eml, extract_mbox
from core.hotkeys import (
    DEFAULTS,
    coerce_hotkeys,
    conflicts,
    normalize_chord,
    read_hotkeys,
    save_hotkeys,
)
from core.images import preprocess_for_ocr, thumbnail_bytes, visual_features
from core.indexer import run_index
from core.ocr import available_engines, ocr_image
from core.privacy import match_reason, purge_indexed
from core.query import ParsedQuery, understand
from core.scanner import iter_entries, kind_for
from core.search import build_match_query, hybrid_search, similar_images
from core.store import Store, _best_copy, _simhash
from core.watch import Watcher

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def make_docx(path: Path, paragraphs: list[str], table: list[list[str]] | None = None) -> None:
    body = "".join(f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>" for text in paragraphs)
    if table:
        rows = "".join(
            "<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>"
            for row in table
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<w:document xmlns:w="{W}"><w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document)


def make_pdf(path: Path, text: str) -> None:
    stream = zlib.compress(f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1"))
    path.write_bytes(
        b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream\nendobj\ntrailer\n%%EOF\n"
    )


def make_screenshot(path: Path, text: str, dark: bool = True) -> None:
    background = (22, 24, 28) if dark else (250, 250, 248)
    foreground = (235, 237, 240) if dark else (25, 25, 25)
    image = Image.new("RGB", (1000, 240), background)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 34)
    except OSError:
        font = ImageFont.load_default()
    draw.text((40, 60), text, font=font, fill=foreground)
    image.save(path)


class ChunkerTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(chunk_text("короткий текст", 1200, 200), ["короткий текст"])

    def test_long_text_splits_with_overlap(self):
        text = " ".join(f"Предложение номер {index} про поиск файлов." for index in range(200))
        chunks = chunk_text(text, 400, 100)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(chunk) <= 460 for chunk in chunks))
        self.assertTrue(all(chunk.strip() == chunk for chunk in chunks))

    def test_hard_split_without_spaces(self):
        chunks = chunk_text("x" * 5000, 1000, 100)
        self.assertGreater(len(chunks), 4)


class ScannerTests(unittest.TestCase):
    def test_kind_detection(self):
        self.assertEqual(kind_for("a.PDF"), "pdf")
        self.assertEqual(kind_for("b.docx"), "docx")
        self.assertEqual(kind_for("c.py"), "code")
        self.assertEqual(kind_for("d.md"), "text")
        self.assertEqual(kind_for("e.png"), "image")
        self.assertEqual(kind_for("f.exe"), "other")


class ExtractorTests(unittest.TestCase):
    def test_docx_paragraphs_and_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.docx"
            make_docx(path, ["Первый абзац", "Второй абзац"], [["Риск", "Мера"], ["OCR", "Апскейл"]])
            text = extract_text(path, "docx")
            self.assertIn("Первый абзац", text)
            self.assertIn("Риск | Мера", text)

    def test_pdf_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.pdf"
            make_pdf(path, "AutoFind local search")
            text = extract_text(path, "pdf")
            self.assertIn("AutoFind local search", text)


class ImageTests(unittest.TestCase):
    def test_visual_features_shape_and_norm(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            make_screenshot(path, "Настройки прокси")
            image = Image.open(path).convert("RGB")
            vector = visual_features(image)
            self.assertEqual(vector.shape, (LAYOUT_DIM,))
            self.assertAlmostEqual(float((vector ** 2).sum()), 1.0, places=4)

    def test_rgb_mode_keeps_colour(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            make_screenshot(path, "тёмная тема", dark=True)
            image = Image.open(path).convert("RGB")
            prepared = preprocess_for_ocr(image, mode="rgb")
            self.assertEqual(prepared.mode, "RGB")
            self.assertLess(sum(prepared.resize((1, 1)).getpixel((0, 0))) / 3, 60)

    def test_gray_mode_inverts_dark(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            make_screenshot(path, "тёмная тема", dark=True)
            image = Image.open(path).convert("RGB")
            prepared = preprocess_for_ocr(image, mode="gray")
            self.assertGreater(sum(prepared.resize((1, 1)).getpixel((0, 0))) / 3, 180)

    def test_thumbnail_is_small_jpeg(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            make_screenshot(path, "скриншот")
            data = thumbnail_bytes(Image.open(path), 200)
            self.assertTrue(data.startswith(b"\xff\xd8"))
            self.assertLess(len(data), 40_000)


@unittest.skipUnless(available_engines(), "no OCR engine on this machine")
class OcrTests(unittest.TestCase):
    def test_recognises_russian_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            make_screenshot(path, "Настройки прокси в браузере", dark=True)
            result = ocr_image(path)
            self.assertIn("прокси", result.text.lower())
            self.assertTrue(result.engine)

    def test_recognises_japanese_text(self):
        """The Windows packs cannot read kana; the bundled model can.

        This is the whole point of the fallback tier: without it the screenshot
        is indexed as a handful of stray Latin letters and is unsearchable.
        """
        from core.ocr import reset_fallbacks
        from core.ocr.engine import downloaded_models

        if "multi" not in downloaded_models():
            self.skipTest("rapidocr multi model not installed")
        font = Path("C:/Windows/Fonts/YuGothR.ttc")
        if not font.exists():
            self.skipTest("no Japanese font to render a test image")

        reset_fallbacks()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "shot.png"
            image = Image.new("RGB", (1280, 420), (250, 250, 248))
            draw = ImageDraw.Draw(image)
            typeface = ImageFont.truetype(str(font), 44)
            draw.text((60, 60), "こんにちは世界", font=typeface, fill=(20, 20, 20))
            draw.text((60, 150), "日本語のテスト", font=typeface, fill=(20, 20, 20))
            draw.text((60, 240), "ファイル検索", font=typeface, fill=(20, 20, 20))
            image.save(path)

            result = ocr_image(path)
            self.assertTrue(result.engine.startswith("rapidocr"), result.engine)
            self.assertIn("世界", result.text)


class SearchTests(unittest.TestCase):
    def test_match_query_is_safe(self):
        query = build_match_query('CLIP "AND" OR*')
        self.assertIn('"clip"*', query)
        self.assertNotIn("OR*", query)

    def test_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "notes.md").write_text(
                "Гибридный поиск объединяет BM25 и векторный поиск ранговым слиянием RRF.", encoding="utf-8"
            )
            (root / "scanner.py").write_text(
                "def scan(root):\n    return [p for p in root.iterdir()]\n", encoding="utf-8"
            )
            make_docx(root / "plan.docx", ["Инсталлятор не должен превышать 150 МБ."])

            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            stats = run_index([root], config, store, embedder)
            self.assertEqual(stats.indexed, 3)
            self.assertGreaterEqual(stats.chunks, 3)

            hits = hybrid_search(store, embedder, "ранговое слияние", limit=5)
            self.assertTrue(hits)
            self.assertIn("notes.md", hits[0].path)

            hits = hybrid_search(store, embedder, "инсталлятор МБ", limit=5)
            self.assertIn("plan.docx", hits[0].path)

            store.close()

    def test_incremental_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "a.txt").write_text("поиск по файлам и скриншотам", encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            first = run_index([root], config, store, embedder)
            second = run_index([root], config, store, embedder)
            self.assertEqual(first.indexed, 1)
            self.assertEqual(second.indexed, 0)
            self.assertEqual(second.skipped, 1)
            store.close()

    def test_filter_by_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "notes.md").write_text("поиск по документам и файлам", encoding="utf-8")
            (root / "tool.py").write_text("def search(query):\n    return query\n", encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            run_index([root], config, store, embedder)

            hits = hybrid_search(store, embedder, "поиск", limit=10, kinds=("code",))
            self.assertTrue(hits)
            self.assertTrue(all(hit.kind == "code" for hit in hits))
            store.close()


    def test_fallback_embedder_does_not_invent_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "notes.md").write_text("поиск по документам и файлам", encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            run_index([root], config, store, embedder)

            self.assertEqual(hybrid_search(store, embedder, "кофемашина эспрессо", limit=5), [])
            self.assertTrue(hybrid_search(store, embedder, "документы", limit=5))
            store.close()


class ImageIndexTests(unittest.TestCase):
    def test_image_gets_thumbnail_and_similar_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            make_screenshot(root / "dark-a.png", "настройки прокси", dark=True)
            make_screenshot(root / "dark-b.png", "параметры сети", dark=True)
            make_screenshot(root / "light.png", "светлая страница", dark=False)

            config = IndexConfig(home=Path(tmp) / "home")
            config.ocr_enabled = False
            embedder = HashingEmbedder()
            visual = LayoutEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            stats = run_index([root], config, store, embedder, visual)

            self.assertEqual(stats.images, 3)
            self.assertEqual(store.stats()["images"], 3)

            row = store.conn.execute("SELECT file_id FROM images WHERE path LIKE '%dark-a.png'").fetchone()
            thumb = store.thumb(int(row["file_id"]))
            self.assertTrue(thumb and thumb.startswith(b"\xff\xd8"))

            similar = similar_images(store, visual, int(row["file_id"]), limit=2)
            self.assertTrue(similar)
            self.assertIn("dark-b.png", similar[0].path)
            store.close()

    def test_duplicate_screenshots_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            make_screenshot(root / "one.png", "одинаковый кадр", dark=True)
            (root / "two.png").write_bytes((root / "one.png").read_bytes())

            config = IndexConfig(home=Path(tmp) / "home")
            config.ocr_enabled = False
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            stats = run_index([root], config, store, embedder, LayoutEmbedder())
            self.assertEqual(stats.images, 1)
            self.assertEqual(stats.duplicates, 1)
            store.close()


class PrivacyTests(unittest.TestCase):
    def test_key_material_is_sensitive(self):
        self.assertIsNotNone(match_reason("/home/me/.ssh/id_ed25519"))
        self.assertIsNotNone(match_reason("/home/me/vault.kdbx"))
        self.assertIsNotNone(match_reason("/srv/app/.env"))
        self.assertIsNotNone(match_reason("/home/me/wallets/electrum.dat"))

    def test_stdlib_module_named_secrets_is_not_sensitive(self):
        self.assertIsNone(match_reason("/opt/blender/python/lib/secrets.py"))
        self.assertIsNone(match_reason("/home/me/project/notes.md"))

    def test_scanner_skips_and_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            (root / "keepass").mkdir(parents=True)
            (root / "keepass" / "vault.txt").write_text("пароль от архива " * 4, encoding="utf-8")
            (root / "passwords.txt").write_text("hunter2 " * 20, encoding="utf-8")
            (root / "notes.md").write_text("обычная заметка про поиск файлов", encoding="utf-8")

            skipped: list[tuple[str, str]] = []
            paths = [entry.path for entry in iter_entries([root], privacy=True, skipped=skipped)]
            self.assertEqual(len(paths), 1)
            self.assertTrue(paths[0].endswith("notes.md"))
            self.assertEqual(len(skipped), 2)

            everything = [entry.path for entry in iter_entries([root], privacy=False)]
            self.assertEqual(len(everything), 3)

    def test_index_reports_sensitive_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "passwords.txt").write_text("hunter2 " * 20, encoding="utf-8")
            (root / "notes.md").write_text("заметка про поиск", encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            stats = run_index([root], config, store, embedder)
            self.assertEqual(stats.sensitive, 1)
            self.assertEqual(stats.indexed, 1)

            purged = purge_indexed(store)
            self.assertEqual(purged["removed"], 0)  # nothing sensitive got in
            store.close()

    def test_categories_select_which_paths_are_hidden(self):
        wallet = "/home/me/wallets/wallet.dat"
        key = "/home/me/.ssh/id_rsa"
        self.assertIsNotNone(match_reason(wallet, {"wallets"}))
        self.assertIsNone(match_reason(wallet, {"keys"}))
        self.assertIsNotNone(match_reason(key, {"keys"}))
        self.assertIsNone(match_reason(key, frozenset()))

    def test_scanner_respects_selected_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            (root / "keepass").mkdir(parents=True)
            (root / "keepass" / "vault.txt").write_text("vault " * 8, encoding="utf-8")
            (root / "notes.md").write_text("обычная заметка про поиск", encoding="utf-8")
            only_keys = [entry.path for entry in iter_entries([root], categories={"keys"})]
            self.assertEqual(len(only_keys), 2)
            only_passwords = [
                entry.path for entry in iter_entries([root], categories={"passwords"})
            ]
            self.assertEqual(len(only_passwords), 1)
            self.assertTrue(only_passwords[0].endswith("notes.md"))

    def test_privacy_prefs_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            config = IndexConfig(home=home)
            config.privacy = False
            config.privacy_categories = frozenset({"keys"})
            save_privacy_prefs(config)
            again = IndexConfig(home=home)
            self.assertFalse(again.privacy)
            self.assertEqual(again.privacy_categories, frozenset({"keys"}))


class WatcherTests(unittest.TestCase):
    def test_diff_detects_changes_and_removals(self):
        watcher = Watcher(["."], IndexConfig(), Path("index.db"), interval=0.1, settle=0)
        previous = {"a": (1, 1.0), "b": (2, 2.0)}
        current = {"a": (1, 1.0), "b": (3, 2.0), "c": (4, 4.0)}
        changed, removed = watcher._diff(previous, current)
        self.assertEqual(sorted(changed), ["b", "c"])
        self.assertEqual(removed, [])

        changed, removed = watcher._diff(current, {"a": (1, 1.0)})
        self.assertEqual(changed, [])
        self.assertEqual(sorted(removed), ["b", "c"])

    def test_batch_indexes_new_file_and_drops_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "a.txt").write_text("первый документ про поиск файлов", encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            config.ocr_enabled = False
            db_path = Path(tmp) / "index.db"
            embedder = HashingEmbedder()
            store = Store(db_path, embedder.name, embedder.dim)
            run_index([root], config, store, embedder)

            watcher = Watcher([root], config, db_path, interval=0.1, settle=0)
            before = watcher._snapshot()
            (root / "b.txt").write_text("второй документ про скриншоты", encoding="utf-8")
            (root / "a.txt").unlink()
            after = watcher._snapshot()

            changed, removed = watcher._diff(before, after)
            self.assertEqual(len(changed), 1)
            self.assertEqual(len(removed), 1)

            watcher._apply(store, embedder, None, changed, removed)
            self.assertEqual(store.stats()["files"], 1)
            self.assertEqual(watcher.state.removed, 1)
            self.assertEqual(watcher.state.added, 1)
            store.close()


    def test_startup_drops_files_deleted_while_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "a.txt").write_text("первый документ про поиск", encoding="utf-8")
            (root / "b.txt").write_text("второй документ про скриншоты", encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            db_path = Path(tmp) / "index.db"
            store = Store(db_path, embedder.name, embedder.dim)
            run_index([root], config, store, embedder)
            self.assertEqual(store.stats()["files"], 2)

            (root / "b.txt").unlink()
            watcher = Watcher([root], config, db_path, interval=0.1, settle=0)
            removed = watcher._drop_missing(store, watcher._snapshot())
            self.assertEqual(removed, 1)
            self.assertEqual(store.stats()["files"], 1)
            store.close()


class CustomTypeTests(unittest.TestCase):
    def test_masks_become_lowercase_basename_globs(self):
        self.assertEqual(normalise_mask("jpg"), "*.jpg")
        self.assertEqual(normalise_mask(" .JPG "), "*.jpg")
        self.assertEqual(normalise_mask("C:\\pics\\*.PNG"), "*.png")
        self.assertEqual(normalise_mask("*.tar.gz"), "*.tar.gz")
        self.assertIsNone(normalise_mask(""))
        self.assertIsNone(normalise_mask("*"))
        self.assertIsNone(normalise_mask("*.*"))
        self.assertIsNone(normalise_mask("."))

    def test_catalog_round_trips_through_a_temp_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            rows = coerce_types(
                [
                    {"name": "Photos", "masks": ["JPG", "*.png", "jpg", "*"]},
                    {"name": "", "masks": ["*.gif"]},
                ]
            )
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].masks, ["*.jpg", "*.png"])
            save_types(home, rows)

            again = read_types(home)
            self.assertEqual([item.name for item in again], ["Photos"])
            self.assertEqual(again[0].masks, ["*.jpg", "*.png"])
            found = find_type(again, again[0].id)
            self.assertIsNotNone(found)
            self.assertIsNone(find_type(again, "missing"))

    def test_two_types_with_the_same_name_get_distinct_ids(self):
        rows = coerce_types(
            [{"name": "Photos", "masks": ["*.jpg"]}, {"name": "Photos", "masks": ["*.png"]}]
        )
        self.assertEqual(len({item.id for item in rows}), 2)

    def test_masks_filter_by_basename(self):
        parsed = ParsedQuery(masks=["*.jpg", "*.png"])
        self.assertTrue(parsed.matches("C:\\pics\\Holiday.JPG", "image", 10, 0.0))
        self.assertTrue(parsed.matches("/home/me/notes.png", "image", 10, 0.0))
        self.assertFalse(parsed.matches("/home/me/report.pdf", "pdf", 10, 0.0))
        # A directory that looks like a match does not make every file inside it one.
        self.assertFalse(parsed.matches("/home/me/holiday.jpg/report.pdf", "pdf", 10, 0.0))
        self.assertTrue(parsed.has_filters)
        self.assertFalse(ParsedQuery(masks=["*.jpg"]).matches("x.gif", "image", 1, 0.0))


class EvalTests(unittest.TestCase):
    def test_matches_accepts_basename_and_fragment(self):
        self.assertTrue(matches("/home/me/docs/plan.docx", "plan.docx"))
        self.assertTrue(matches("/home/me/docs/plan.docx", "docs/plan"))
        self.assertFalse(matches("/home/me/docs/plan.docx", "other.md"))

    def test_recall_and_mrr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            (root / "rrf.md").write_text("Ранговое слияние объединяет два списка результатов.", encoding="utf-8")
            (root / "ocr.md").write_text("Распознавание текста со скриншотов через движок системы.", encoding="utf-8")
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            run_index([root], config, store, embedder)

            labels = {
                "label": "unit",
                "queries": [
                    {"q": "ранговое слияние", "expected": ["rrf.md"]},
                    {"q": "совершенно посторонний запрос про пингвинов", "expected": ["missing.md"]},
                ],
            }
            report = evaluate(store, embedder, labels, limit=10)
            self.assertEqual(report.n, 2)
            self.assertAlmostEqual(report.recall, 0.5, places=3)
            self.assertAlmostEqual(report.mrr, 0.5, places=3)
            self.assertEqual(report.scores[0].rank, 1)
            self.assertIsNone(report.scores[1].rank)
            store.close()


class DoctorTests(unittest.TestCase):
    def test_bundle_estimate_is_internally_consistent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = IndexConfig(home=Path(tmp) / "home")
            bundle = estimate_bundle(config)
            total = round(sum(part["mb"] for part in bundle["parts"]), 1)
            self.assertAlmostEqual(bundle["total_mb"], total, places=1)
            self.assertEqual(bundle["fits"], bundle["total_mb"] <= bundle["budget_mb"])
            self.assertTrue(any(part["part"].startswith("model:") for part in bundle["parts"]))


class HotkeyTests(unittest.TestCase):
    def test_chords_are_canonical(self):
        self.assertEqual(normalize_chord("Shift + Ctrl + D"), "ctrl+shift+d")
        self.assertEqual(normalize_chord("CTRL+K"), "ctrl+k")
        self.assertEqual(normalize_chord("Return"), "enter")
        self.assertEqual(normalize_chord("Space"), "space")
        self.assertEqual(normalize_chord("Spacebar"), "space")
        self.assertEqual(normalize_chord("+"), "plus")
        # A lone modifier is not a shortcut, and two keys is not a chord.
        self.assertIsNone(normalize_chord("ctrl"))
        self.assertIsNone(normalize_chord("ctrl+a+b"))
        self.assertIsNone(normalize_chord(""))

    def test_coerce_keeps_defaults_and_empty_disables(self):
        clean = coerce_hotkeys({"open": [], "reveal": ["Shift+Enter"]})
        self.assertEqual(clean["open"], [])
        self.assertEqual(clean["reveal"], ["shift+enter"])
        self.assertEqual(clean["focus"], DEFAULTS["focus"])

    def test_round_trip_through_a_temp_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.assertEqual(read_hotkeys(home), DEFAULTS)
            saved = save_hotkeys(home, {"export": ["Ctrl+Shift+E", "ctrl+shift+e"]})
            self.assertEqual(saved["export"], ["ctrl+shift+e"])
            self.assertEqual(read_hotkeys(home)["export"], ["ctrl+shift+e"])

    def test_conflicts_report_duplicates_and_similar_pairs(self):
        hotkeys = {action: list(combos) for action, combos in DEFAULTS.items()}
        hotkeys["open"] = ["ctrl+shift+d"]
        hotkeys["select"] = ["ctrl+d"]
        found = conflicts(hotkeys)
        kinds = {(row["type"], row["a"]["action"], row["b"]["action"]) for row in found}
        self.assertIn(("duplicate", "duplicates", "open"), kinds)
        self.assertTrue(any(row["type"] == "similar" for row in found))


class TagCollectionTests(unittest.TestCase):
    def _store_with_files(self, tmp: str, names: list[str]) -> tuple[Store, list[int]]:
        root = Path(tmp) / "corpus"
        root.mkdir()
        for name in names:
            (root / name).write_text(f"{name} содержит общий текст для поиска", encoding="utf-8")
        config = IndexConfig(home=Path(tmp) / "home")
        embedder = HashingEmbedder()
        store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
        run_index([root], config, store, embedder)
        ids = [int(row["id"]) for row in store.conn.execute("SELECT id FROM files ORDER BY path")]
        return store, ids

    def test_tags_are_per_file_and_and_filtered(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, ids = self._store_with_files(tmp, ["a.txt", "b.txt", "c.txt"])
            store.set_tag(ids[0], "Work")
            store.set_tag(ids[0], "Urgent")
            store.set_tag(ids[1], "work")
            self.assertEqual(store.tags_for(ids[:2])[ids[0]], ["Urgent", "Work"])
            self.assertEqual(store.file_ids_for_tags(["WORK"]), {ids[0], ids[1]})
            self.assertEqual(store.file_ids_for_tags(["work", "urgent"]), {ids[0]})
            counts = {row["tag"]: row["count"] for row in store.all_tags()}
            self.assertEqual(counts, {"Urgent": 1, "Work": 2})
            store.set_tag(ids[0], "urgent", on=False)
            self.assertNotIn("Urgent", store.tags_for([ids[0]])[ids[0]])
            store.close()

    def test_collections_are_union_and_names_are_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, ids = self._store_with_files(tmp, ["a.txt", "b.txt", "c.txt"])
            first = store.create_collection("Projects")
            again = store.create_collection("projects")
            self.assertEqual(first, again)
            second = store.create_collection("Archive")
            store.add_to_collection(first, [ids[0], ids[1]])
            store.add_to_collection(second, [ids[2]])
            self.assertEqual(store.collection_ids_for_names(["PROJECTS"]), {ids[0], ids[1]})
            self.assertEqual(
                store.collection_ids_for_names(["Projects", "Archive"]), set(ids)
            )
            names = {row["name"]: row["count"] for row in store.collections()}
            self.assertEqual(names, {"Archive": 1, "Projects": 2})
            store.close()

    def test_query_understands_tag_and_collection_filters(self):
        parsed = understand('отчёт tag:work tag:"важное" collection:"Проекты"')
        self.assertEqual(parsed.tags, ["work", "важное"])
        self.assertEqual(parsed.collections, ["Проекты"])
        self.assertIn(("tag", "work"), {(chip.kind, chip.value) for chip in parsed.chips})
        self.assertTrue(parsed.has_filters)


class DocumentDuplicateTests(unittest.TestCase):
    def test_simhash_is_stable_and_separates_different_text(self):
        text = "один два три четыре пять шесть семь восемь девять десять одиннадцать двенадцать"
        self.assertEqual(_simhash(text), _simhash(text))
        self.assertNotEqual(
            _simhash(text),
            _simhash("совершенно другой набор слов про погоду море солнце пляж отпуск лето"),
        )
        # Too little text cannot be fingerprinted, so it never groups by accident.
        self.assertEqual(_simhash("короткий текст"), 0)

    def test_best_copy_prefers_a_shallow_recent_file(self):
        items = [
            {"file_id": 1, "path": "/home/me/Temp/plan.docx", "size": 10, "mtime": 99.0},
            {"file_id": 2, "path": "/home/me/docs/plan.docx", "size": 10, "mtime": 50.0},
        ]
        self.assertEqual(_best_copy(items)["file_id"], 2)
        # For documents the larger copy wins when everything else ties.
        items = [
            {"file_id": 3, "path": "/home/me/docs/a", "size": 10, "mtime": 5.0},
            {"file_id": 4, "path": "/home/me/docs/b", "size": 40, "mtime": 5.0},
        ]
        self.assertEqual(_best_copy(items, prefer_size=True)["file_id"], 4)

    def test_near_identical_documents_are_grouped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "corpus"
            root.mkdir()
            body = (
                "Инсталлятор не должен превышать сто пятьдесят мегабайт. "
                "Сборка запускается одной командой, а журнал пишется рядом с архивом. "
                "Перед установкой проверяется свободное место и версия системы. "
                "После распаковки приложение само открывает окно приветствия."
            )
            (root / "plan.txt").write_text(body, encoding="utf-8")
            (root / "plan-copy.md").write_text(body, encoding="utf-8")
            (root / "other.txt").write_text(
                "Совершенно другой документ про погоду, море, солнце, пляж и отпуск. "
                "Здесь нет ни слова про инсталлятор, сборку, журнал или распаковку. "
                "Поэтому отпечаток текста обязан отличаться от предыдущего документа.",
                encoding="utf-8",
            )
            config = IndexConfig(home=Path(tmp) / "home")
            embedder = HashingEmbedder()
            store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
            run_index([root], config, store, embedder)
            groups = store.duplicate_documents()
            self.assertEqual(len(groups), 1)
            names = {Path(item["path"]).name for item in groups[0]["items"]}
            self.assertEqual(names, {"plan.txt", "plan-copy.md"})
            self.assertIn(groups[0]["keep"], {item["file_id"] for item in groups[0]["items"]})

            # The streaming endpoint turns these callbacks into percentages, so
            # the count must start at zero, only grow, and finish at the total.
            events = []
            store.duplicate_documents(
                progress=lambda done, total: events.append((done, total))
            )
            self.assertEqual(events[0], (0, 3))
            self.assertEqual(events[-1], (3, 3))
            self.assertEqual(events, sorted(events))
            self.assertTrue(all(total == 3 for _, total in events))
            store.close()


class ArchiveEmailTests(unittest.TestCase):
    def test_zip_and_gzip_text_are_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "notes.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("readme.txt", "внутри архива лежит важный текст")
                bundle.writestr("image.png", b"\x89PNG\r\n\x1a\n\x00\x00")
            text = extract_archive(archive)
            self.assertIn("важный текст", text)
            self.assertIn("readme.txt", text)
            self.assertNotIn("PNG", text)

            single = root / "lines.txt.gz"
            with gzip.open(single, "wb") as handle:
                handle.write("сжатая строка с текстом".encode("utf-8"))
            self.assertIn("сжатая строка", extract_archive(single))

    def test_eml_reduces_html_to_readable_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mail.eml"
            path.write_bytes(
                "Subject: Счёт за август\r\n"
                "From: billing@example.com\r\n"
                "To: me@example.com\r\n"
                "Content-Type: text/html; charset=utf-8\r\n"
                "\r\n"
                "<html><body><p>К оплате <b>1200</b> рублей.</p>"
                "<style>.x{display:none}</style></body></html>\r\n".encode("utf-8")
            )
            text = extract_eml(path)
            self.assertIn("Счёт за август", text)
            self.assertIn("billing@example.com", text)
            self.assertIn("1200", text)
            self.assertNotIn("<b>", text)
            self.assertNotIn("display:none", text)

    def test_mbox_reads_every_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "box.mbox"
            path.write_text(
                "From sender@example.com Mon Jan  1 00:00:00 2024\n"
                "Subject: Первое письмо\n"
                "From: a@example.com\n"
                "\n"
                "текст первого письма\n"
                "\n"
                "From sender@example.com Mon Jan  2 00:00:00 2024\n"
                "Subject: Второе письмо\n"
                "From: b@example.com\n"
                "\n"
                "текст второго письма\n",
                encoding="utf-8",
            )
            text = extract_mbox(path)
            self.assertIn("Первое письмо", text)
            self.assertIn("Второе письмо", text)
            self.assertIn("текст второго письма", text)


if __name__ == "__main__":
    unittest.main()
