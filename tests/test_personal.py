"""Pins, saved searches, the dictionary, definitions and the kensaku import.

The point of these four is that they are the things a person brings with them
from another search app. A test that only checks the SQL would miss the part
that matters, so most of these go through the same path the interface uses:
parse a query, rank it, look at what the chip row would say.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from core.config import IndexConfig
from core.embed import HashingEmbedder
from core.indexer import run_index
from core.migrate import apply as apply_import
from core.migrate import read as read_config
from core.personal import (
    MAX_EXPANSIONS,
    expand_aliases,
    forget,
    save,
    teach,
    toggle_pin,
)
from core.query import understand
from core.search import search_page
from core.store import Store, normalize_path
from core.symbols import extract_for, language_for

PYTHON_SOURCE = '''"""Module docstring."""

import os


class Parser:
    """Not a definition to find."""

    def parse_query(self, text):
        return text


def parse_query(text):
    return text.split()


def _parse_query_legacy(text):
    return [text]
'''

TS_SOURCE = """export function parseQuery(input: string) {
  return input.trim();
}

export class QueryParser {
  run() { return null; }
}
"""

RUST_SOURCE = """pub struct Parser {
    items: Vec<String>,
}

pub fn parse_query(text: &str) -> Vec<&str> {
    text.split(' ').collect()
}
"""

GO_SOURCE = """package main

type Parser struct {
    Items []string
}

func parseQuery(text string) []string {
    return nil
}
"""


class SymbolExtractionTests(unittest.TestCase):
    def test_python_definitions(self):
        names = {(symbol.name, symbol.kind) for symbol in extract_for("a.py", PYTHON_SOURCE)}
        self.assertIn(("Parser", "class"), names)
        self.assertIn(("parse_query", "function"), names)
        self.assertIn(("_parse_query_legacy", "function"), names)

    def test_line_numbers_point_at_the_definition(self):
        symbols = {symbol.name: symbol for symbol in extract_for("a.py", PYTHON_SOURCE)}
        lines = PYTHON_SOURCE.splitlines()
        self.assertEqual(lines[symbols["parse_query"].line - 1].strip(), "def parse_query(text):")

    def test_typescript_and_rust_and_go(self):
        ts = {(symbol.name, symbol.kind) for symbol in extract_for("a.ts", TS_SOURCE)}
        self.assertIn(("parseQuery", "function"), ts)
        self.assertIn(("QueryParser", "class"), ts)
        rust = {(symbol.name, symbol.kind) for symbol in extract_for("a.rs", RUST_SOURCE)}
        self.assertIn(("Parser", "struct"), rust)
        self.assertIn(("parse_query", "function"), rust)
        go = {(symbol.name, symbol.kind) for symbol in extract_for("a.go", GO_SOURCE)}
        self.assertIn(("parseQuery", "function"), go)
        self.assertIn(("Parser", "type"), go)

    def test_comments_are_not_definitions(self):
        source = "# def not_real():\n// function alsoNot() {}\n"
        names = {symbol.name for symbol in extract_for("a.py", source)}
        self.assertNotIn("not_real", names)

    def test_unknown_extension_yields_nothing(self):
        self.assertIsNone(language_for("notes.xyz"))
        self.assertEqual(extract_for("notes.xyz", "def parse_query(): pass"), [])

    def test_markdown_is_not_code(self):
        self.assertEqual(extract_for("readme.md", "# def parse_query():"), [])


class GrammarTests(unittest.TestCase):
    def test_symbol_key_reaches_the_parser(self):
        query = understand("sym:parse_query")
        self.assertEqual(query.symbols, ["parse_query"])
        self.assertTrue(query.has_filters)
        kinds = {chip.kind for chip in query.chips}
        self.assertIn("symbol", kinds)

    def test_aliases_of_the_key_all_work(self):
        for spelling in ("sym:", "symbol:", "def:", "fn:"):
            with self.subTest(spelling=spelling):
                self.assertEqual(understand(f"{spelling}parse").symbols, ["parse"])

    def test_symbol_is_not_a_listing(self):
        # `sym:x` must not fall into the "pure filter, list everything" path.
        query = understand("sym:parse")
        self.assertFalse(query.mode == "exhaustive" and not query.symbols)


class PersonalBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "corpus"
        self.root.mkdir()
        (self.root / "engine.py").write_text(PYTHON_SOURCE, encoding="utf-8")
        (self.root / "app.ts").write_text(TS_SOURCE, encoding="utf-8")
        (self.root / "notes.txt").write_text(
            "invoice for march, резюме attached\n", encoding="utf-8"
        )
        self.config = IndexConfig(home=Path(self.tmp.name) / "home")
        self.embedder = HashingEmbedder()
        self.db = Path(self.tmp.name) / "index.db"
        store = Store(self.db, self.embedder.name, self.embedder.dim)
        run_index([self.root], self.config, store, self.embedder)
        store.close()

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, readonly: bool = True) -> Store:
        return Store(self.db, self.embedder.name, self.embedder.dim, readonly=readonly)

    def file_id(self, name: str) -> int:
        store = self.store()
        try:
            for row in store.all_files():
                if row["path"].endswith(name):
                    return int(row["id"])
            raise AssertionError(f"{name} is not in the index")
        finally:
            store.close()


class SymbolSearchTests(PersonalBase):
    def test_definitions_are_indexed(self):
        store = self.store()
        try:
            self.assertGreater(store.symbol_count(), 0)
        finally:
            store.close()

    def test_exact_name_ranks_above_the_legacy_variant(self):
        store = self.store()
        try:
            rows = store.find_symbols("parse_query", 10)
        finally:
            store.close()
        names = [row["name"] for row in rows]
        self.assertEqual(names[0], "parse_query")
        self.assertIn("_parse_query_legacy", names)

    def test_search_by_symbol_returns_the_definition_as_the_snippet(self):
        store = self.store()
        try:
            page = search_page(store, self.embedder, "sym:parse_query")
        finally:
            store.close()
        self.assertTrue(page.hits)
        hit = page.hits[0]
        self.assertIn("function parse_query", hit.symbol)
        self.assertIn("line", hit.snippet)

    def test_prefix_finds_every_family(self):
        store = self.store()
        try:
            page = search_page(store, self.embedder, "sym:parse")
        finally:
            store.close()
        names = {hit.name for hit in page.hits}
        self.assertIn("engine.py", names)
        self.assertIn("app.ts", names)


class PinTests(PersonalBase):
    def test_pin_and_unpin_round_trip(self):
        store = self.store(readonly=False)
        try:
            file_id = self.file_id("notes.txt")
            self.assertTrue(toggle_pin(store, file_id))
            store.commit()
            self.assertIn(file_id, store.pinned_ids())
            self.assertFalse(toggle_pin(store, file_id))
            store.commit()
            self.assertNotIn(file_id, store.pinned_ids())
        finally:
            store.close()

    def test_a_pin_is_a_bonus_not_a_filter(self):
        # Pinning one file must not hide the others: a pin says "I care about
        # this", not "show me only this".
        store = self.store(readonly=False)
        try:
            store.set_pin(self.file_id("notes.txt"), True)
            store.commit()
        finally:
            store.close()
        store = self.store()
        try:
            page = search_page(store, self.embedder, "invoice")
        finally:
            store.close()
        self.assertTrue(page.hits)
        self.assertTrue(page.hits[0].pinned)
        self.assertEqual(page.hits[0].name, "notes.txt")

    def test_pin_survives_a_filter_listing(self):
        store = self.store(readonly=False)
        try:
            store.set_pin(self.file_id("engine.py"), True)
            store.commit()
        finally:
            store.close()
        store = self.store()
        try:
            page = search_page(store, self.embedder, "type:code")
        finally:
            store.close()
        pinned = [hit.name for hit in page.hits if hit.pinned]
        self.assertEqual(pinned, ["engine.py"])

    def test_pinned_rows_carry_the_file(self):
        store = self.store(readonly=False)
        try:
            store.set_pin(self.file_id("app.ts"), True)
            store.commit()
            rows = store.pinned_rows()
        finally:
            store.close()
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["path"].endswith("app.ts"))
        self.assertEqual(rows[0]["kind"], "code")


class SavedSearchTests(PersonalBase):
    def test_saving_twice_updates_the_label_instead_of_duplicating(self):
        store = self.store(readonly=False)
        try:
            first = save(store, "type:pdf   invoices", "Invoices")
            second = save(store, "type:pdf invoices", "March invoices")
            rows = store.saved_searches()
        finally:
            store.close()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "March invoices")

    def test_whitespace_is_normalised(self):
        store = self.store(readonly=False)
        try:
            item = save(store, "  type:code   parse  ")
        finally:
            store.close()
        self.assertEqual(item["query"], "type:code parse")

    def test_empty_query_is_rejected(self):
        store = self.store(readonly=False)
        try:
            with self.assertRaises(ValueError):
                save(store, "   ")
        finally:
            store.close()


class AliasTests(PersonalBase):
    def test_expansion_is_whole_word_and_case_insensitive(self):
        store = self.store(readonly=False)
        try:
            teach(store, "cv", "резюме")
        finally:
            store.close()
        store = self.store()
        try:
            text, applied = expand_aliases(store, "CV attached")
        finally:
            store.close()
        self.assertEqual(text, "резюме attached")
        self.assertEqual([item.alias for item in applied], ["cv"])

    def test_a_filter_value_is_never_expanded(self):
        # `type:cv` asks for `.cv` files; rewriting it to `type:резюме` would be
        # a silent wrong answer, which is worse than no dictionary at all.
        store = self.store(readonly=False)
        try:
            teach(store, "cv", "резюме")
        finally:
            store.close()
        store = self.store()
        try:
            text, applied = expand_aliases(store, "type:cv")
        finally:
            store.close()
        self.assertEqual(text, "type:cv")
        self.assertEqual(applied, [])

    def test_a_longer_word_is_not_hijacked(self):
        store = self.store(readonly=False)
        try:
            teach(store, "cv", "резюме")
        finally:
            store.close()
        store = self.store()
        try:
            text, applied = expand_aliases(store, "cvs and cv-notes")
        finally:
            store.close()
        self.assertEqual(text, "cvs and cv-notes")
        self.assertEqual(applied, [])

    def test_expansion_is_capped(self):
        store = self.store(readonly=False)
        try:
            for index in range(MAX_EXPANSIONS + 4):
                teach(store, f"w{index}", f"word{index}")
        finally:
            store.close()
        query = " ".join(f"w{index}" for index in range(MAX_EXPANSIONS + 4))
        store = self.store()
        try:
            _, applied = expand_aliases(store, query)
        finally:
            store.close()
        self.assertEqual(len(applied), MAX_EXPANSIONS)

    def test_an_alias_shows_up_as_a_chip(self):
        store = self.store(readonly=False)
        try:
            teach(store, "cv", "резюме")
        finally:
            store.close()
        store = self.store()
        try:
            page = search_page(store, self.embedder, "cv")
        finally:
            store.close()
        kinds = [chip["kind"] for chip in page.chips]
        self.assertEqual(kinds[0], "alias")
        self.assertTrue(any(hit.name == "notes.txt" for hit in page.hits))

    def test_forget_removes_it(self):
        store = self.store(readonly=False)
        try:
            teach(store, "inv", "invoice")
            self.assertTrue(forget(store, "inv"))
            self.assertEqual(store.aliases(), {})
        finally:
            store.close()

    def test_an_alias_must_be_one_word(self):
        store = self.store(readonly=False)
        try:
            with self.assertRaises(ValueError):
                teach(store, "two words", "something")
        finally:
            store.close()


class PathNormalisationTests(unittest.TestCase):
    def test_case_and_separators_do_not_matter(self):
        self.assertEqual(normalize_path("C:\\Docs\\A.PDF"), normalize_path("c:/docs/a.pdf"))

    def test_repeated_separators_collapse(self):
        self.assertEqual(normalize_path("C://Docs//a.pdf"), normalize_path("C:/Docs/a.pdf"))

    def test_trailing_separator_is_dropped(self):
        self.assertEqual(normalize_path("C:/Docs/"), normalize_path("C:/Docs"))


class ImportTests(PersonalBase):
    """`semfind import kensaku` — the reason nobody has to retype anything."""

    def write_config(self, payload: dict) -> Path:
        path = Path(self.tmp.name) / "kensaku" / "config.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def test_json_config_is_understood(self):
        config = self.write_config(
            {
                "pinned": [
                    str(self.root / "engine.py"),
                    {"path": str(self.root / "app.ts")},
                ],
                "savedSearches": [{"query": "type:pdf invoices", "label": "Invoices"}],
                "settings": {"dictionary": {"cv": "резюме"}},
            }
        )
        source = read_config(config)
        self.assertEqual(len(source.pins), 2)
        self.assertEqual(len(source.saved), 1)
        self.assertEqual(source.aliases, {"cv": "резюме"})

    def test_dry_run_writes_nothing(self):
        config = self.write_config({"pinned": [str(self.root / "engine.py")]})
        store = self.store(readonly=False)
        try:
            report = apply_import(store, [read_config(config)], write=False)
        finally:
            store.close()
        self.assertEqual(report["pins"], 1)
        store = self.store()
        try:
            self.assertEqual(store.pinned_ids(), set())
        finally:
            store.close()

    def test_write_moves_pins_saved_and_aliases(self):
        config = self.write_config(
            {
                "pinned": [str(self.root / "engine.py")],
                "savedSearches": ["screenshot last week"],
                "dictionary": {"inv": "invoice"},
            }
        )
        store = self.store(readonly=False)
        try:
            report = apply_import(store, [read_config(config)], write=True)
        finally:
            store.close()
        self.assertEqual(report["pins"], 1)
        self.assertEqual(report["saved"], 1)
        self.assertEqual(report["aliases"], 1)
        store = self.store()
        try:
            self.assertEqual(len(store.pinned_ids()), 1)
            self.assertEqual(len(store.saved_searches()), 1)
            self.assertEqual(store.aliases(), {"inv": "invoice"})
        finally:
            store.close()

    def test_a_pin_that_is_not_indexed_is_reported_not_dropped(self):
        missing = str(self.root / "gone.py")
        config = self.write_config({"pinned": [str(self.root / "engine.py"), missing]})
        store = self.store(readonly=False)
        try:
            report = apply_import(store, [read_config(config)], write=True)
        finally:
            store.close()
        self.assertEqual(report["pins"], 1)
        self.assertEqual(report["missing"], [missing])

    def test_sqlite_config_is_understood(self):
        # kensaku is a Tauri app and its store may be a database rather than a
        # JSON file; the reader has to cope with both without a schema.
        path = Path(self.tmp.name) / "kensaku" / "index.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE pinned_files (path TEXT, added INTEGER)")
            conn.execute("CREATE TABLE aliases (alias TEXT, expand TEXT)")
            conn.execute(
                "INSERT INTO pinned_files VALUES (?, ?)",
                (str(self.root / "engine.py"), time.time()),
            )
            conn.execute("INSERT INTO aliases VALUES ('cv', 'резюме')")
            conn.commit()
        finally:
            conn.close()
        source = read_config(path)
        self.assertEqual(source.kind, "sqlite")
        self.assertEqual(len(source.pins), 1)
        self.assertEqual(source.aliases, {"cv": "резюме"})


class MissingTableTests(PersonalBase):
    """An index from before the personal layer must still open — and nothing else."""

    def test_a_missing_table_reads_as_empty(self):
        store = self.store(readonly=False)
        try:
            store.conn.execute("DROP TABLE pins")
            self.assertEqual(store.pinned_ids(), set())
            self.assertEqual(store.pinned_rows(), [])
        finally:
            store.close()

    def test_a_locked_database_is_not_an_empty_answer(self):
        # The guard exists for an old schema. A busy index raises the same
        # exception class, and reporting it as "nothing pinned" would be a wrong
        # answer that looks like a real one.
        from core.store import _reraises_unless_missing

        with self.assertRaises(sqlite3.OperationalError):
            _reraises_unless_missing(sqlite3.OperationalError("database is locked"))
        self.assertIsNone(_reraises_unless_missing(sqlite3.OperationalError("no such table: pins")))


class McpTests(PersonalBase):
    """The same three things reach an agent, not only the window."""

    def setUp(self):
        super().setUp()
        self._servers = []

    def server(self):
        from core.mcp import McpServer

        server = McpServer(self.db, self.config)
        self._servers.append(server)
        return server

    def tearDown(self):
        # The server keeps its store open between calls, which is right for a
        # session and wrong for a temp directory that is about to be deleted.
        # Closing here rather than in a cleanup matters: `addCleanup` runs after
        # `tearDown`, so the file would still be locked when the directory goes.
        for server in getattr(self, "_servers", []):
            if server._store is not None:
                server._store.close()
                server._store = None
        super().tearDown()

    def call(self, tool: str, **arguments):
        response = self.server().handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        })
        return json.loads(response["result"]["content"][0]["text"])

    def test_find_symbol_is_a_tool(self):
        server = self.server()
        names = [
            item["name"]
            for item in server.handle(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            )["result"]["tools"]
        ]
        self.assertIn("find_symbol", names)

    def test_find_symbol_answers_with_the_definition(self):
        data = self.call("find_symbol", name="parse_query")
        self.assertTrue(data["definitions"])
        first = data["definitions"][0]
        self.assertEqual(first["symbol"], "parse_query")
        self.assertEqual(first["kind"], "function")
        self.assertTrue(first["source"].startswith("def parse_query"))

    def test_an_alias_works_over_mcp_too(self):
        store = self.store(readonly=False)
        try:
            teach(store, "inv", "invoice")
        finally:
            store.close()
        data = self.call("search", query="inv")
        self.assertEqual(data["interpreted_as"][0], "inv → invoice")


if __name__ == "__main__":
    unittest.main()
