"""Regressions for the MCP audit findings: batch, negotiation, cursors, version."""

import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from core import mcp
from core.__version__ import __version__
from core.config import IndexConfig


class McpAuditTests(unittest.TestCase):
    def setUp(self):
        self.server = mcp.McpServer(Path("missing.db"), IndexConfig())

    def test_initialize_echoes_supported_client_version(self):
        response = self.server.handle({
            "method": "initialize", "id": 1,
            "params": {"protocolVersion": "2025-06-18"},
        })
        self.assertEqual(response["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(response["result"]["serverInfo"]["version"], __version__)

    def test_initialize_falls_back_for_unknown_version(self):
        response = self.server.handle({
            "method": "initialize", "id": 2,
            "params": {"protocolVersion": "1999-01-01"},
        })
        self.assertEqual(response["result"]["protocolVersion"], mcp.PROTOCOL_VERSION)

    def test_cursor_round_trip(self):
        for offset in (0, 7, 9_999_999, 123_456_789):
            with self.subTest(offset=offset):
                self.assertEqual(mcp._decode_cursor(mcp._encode_cursor(offset)),
                                 min(offset, 9_999_999))

    def test_batch_requests_produce_batch_responses(self):
        lines = json.dumps([
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]) + "\n"
        original = mcp.sys.stdin
        mcp.sys.stdin = io.StringIO(lines)
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                self.assertEqual(self.server.run(), 0)
        finally:
            mcp.sys.stdin = original
        batch = json.loads(out.getvalue().strip())
        self.assertIsInstance(batch, list)
        self.assertEqual([row["id"] for row in batch], [1, 2])


if __name__ == "__main__":
    unittest.main()
