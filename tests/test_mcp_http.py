"""MCP transports, client wiring, and the CJK OCR router."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import connect
from core.config import IndexConfig
from core.mcp import McpServer
from core.ocr import script


class ConnectTests(unittest.TestCase):
    def _config(self, tmp):
        config = IndexConfig(home=Path(tmp) / "home")
        config.ensure_home()
        return config

    def test_token_is_stable_and_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            first = connect.token(config)
            second = connect.token(config)
            self.assertEqual(first, second)
            self.assertGreaterEqual(len(first), 32)
            self.assertTrue((config.home / connect.TOKEN_FILE).exists())

    def test_stdio_entry_does_not_depend_on_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            entry = connect.entry("stdio", config, 8765, Path(tmp) / "index.db")
            self.assertIn("command", entry)
            blob = " ".join(str(part) for part in entry.get("args", []))
            # Either an absolute console script, or a bootstrap that adds the
            # project root to sys.path — never a bare `-m core.cli`.
            self.assertTrue(entry["command"] or blob)
            if entry["command"].endswith(("python.exe", "python")):
                self.assertIn("sys.path.insert", blob)
            self.assertIn("mcp", blob)

    def test_http_entry_carries_a_bearer_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            entry = connect.entry("http", config, 8899, Path(tmp) / "index.db")
            self.assertEqual(entry["url"], "http://127.0.0.1:8899/mcp")
            self.assertTrue(entry["headers"]["Authorization"].startswith("Bearer "))
            self.assertIn(connect.token(config), entry["headers"]["Authorization"])

    def test_merge_keeps_other_servers(self):
        existing = {"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}
        merged = connect.merge(existing, {"command": "y"})
        self.assertEqual(merged["mcpServers"]["other"], {"command": "x"})
        self.assertEqual(merged["mcpServers"][connect.SERVER_KEY], {"command": "y"})
        self.assertEqual(merged["theme"], "dark")

    def test_write_backs_up_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = self._config(tmp)
            target = connect.Target("cursor", "Cursor", Path(tmp) / "mcp.json")
            target.path.write_text('{"mcpServers": {"other": {"command": "x"}}}', encoding="utf-8")
            changed, _ = connect.write(target, {"command": "autofind"})
            self.assertTrue(changed)
            payload = json.loads(target.path.read_text(encoding="utf-8"))
            self.assertIn("other", payload["mcpServers"])
            self.assertEqual(payload["mcpServers"]["autofind"], {"command": "autofind"})
            self.assertTrue(target.path.with_suffix(".json.bak").exists())

            again, _ = connect.write(target, {"command": "autofind"})
            self.assertTrue(again)
            self.assertEqual(
                json.loads(target.path.read_text(encoding="utf-8"))["mcpServers"]["autofind"],
                {"command": "autofind"},
            )

    def test_broken_config_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = connect.Target("cursor", "Cursor", Path(tmp) / "mcp.json")
            target.path.write_text("{not json", encoding="utf-8")
            changed, message = connect.write(target, {"command": "autofind"})
            self.assertFalse(changed)
            self.assertIn("cannot read", message)
            self.assertEqual(target.path.read_text(encoding="utf-8"), "{not json")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = connect.Target("cursor", "Cursor", Path(tmp) / "mcp.json")
            changed, message = connect.write(target, {"command": "autofind"}, dry_run=True)
            self.assertTrue(changed)
            self.assertIn("autofind", message)
            self.assertFalse(target.path.exists())


class McpHttpShapeTests(unittest.TestCase):
    """The handler must answer the transport calls a client makes on connect."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = McpServer(Path(self.tmp.name) / "index.db", IndexConfig(home=Path(self.tmp.name)))

    def tearDown(self):
        if self.server._store is not None:
            self.server._store.close()
        self.tmp.cleanup()

    def test_initialize_then_tools_list(self):
        init = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        })
        self.assertEqual(init["result"]["serverInfo"]["name"], "autofind")
        listing = self.server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [tool["name"] for tool in listing["result"]["tools"]]
        self.assertIn("search", names)
        self.assertIn("index_state", names)

    def test_notification_returns_none(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_unknown_method_is_an_error(self):
        response = self.server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_bad_tool_arguments_do_not_raise(self):
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "search", "arguments": {}},
        })
        self.assertTrue(response["result"]["isError"])


class ScriptTests(unittest.TestCase):
    def test_cjk_detection(self):
        self.assertTrue(script.is_cjk("你"))
        self.assertTrue(script.is_cjk("あ"))
        self.assertTrue(script.is_cjk("한"))
        self.assertFalse(script.is_cjk("R"))
        self.assertFalse(script.is_cjk("ы"))

    def test_cjk_ratio(self):
        self.assertAlmostEqual(script.cjk_ratio("你好世界"), 1.0)
        self.assertEqual(script.cjk_ratio(""), 0.0)
        self.assertLess(script.cjk_ratio("hello 你好"), 0.6)

    def test_score_weights_ideographs(self):
        self.assertGreater(script.text_score("你好世界"), script.text_score("abcd"))

    def test_failed_read_is_detected(self):
        # 1920x1080 of a chat window returns hundreds of characters; a failed
        # read returns a handful of stray Latin letters.
        self.assertTrue(script.looks_unread("", 1920, 1080))
        self.assertTrue(script.looks_unread("IIl", 1920, 1080))
        self.assertFalse(script.looks_unread("Бюджет проекта 2026 Статья Разработка Дизайн", 1920, 1080))

    def test_replacement_needs_a_real_improvement(self):
        self.assertFalse(script.better("hello world", "hello worlds"))
        self.assertTrue(script.better("IIl", "你好世界文件搜索"))


if __name__ == "__main__":
    unittest.main()
