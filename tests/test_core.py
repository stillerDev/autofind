"""Unit tests. Runs on stdlib unittest: python -m unittest discover -s tests -t ."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from core.chunker import chunk_text
from core.config import IndexConfig
from core.doctor import estimate_bundle
from core.embed import HashingEmbedder
from core.embed.visual import LAYOUT_DIM, LayoutEmbedder
from core.evaluate import evaluate, matches
from core.extract import extract_text
from core.images import preprocess_for_ocr, thumbnail_bytes, visual_features
from core.indexer import run_index
from core.ocr import available_engines, ocr_image
from core.privacy import match_reason, purge_indexed
from core.scanner import iter_entries, kind_for
from core.search import build_match_query, hybrid_search, similar_images
from core.store import Store
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
            make_pdf(path, "SemFind local search")
            text = extract_text(path, "pdf")
            self.assertIn("SemFind local search", text)


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


if __name__ == "__main__":
    unittest.main()
