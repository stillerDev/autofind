"""Outlook .msg: kind routing, optional dependency, graceful degradation."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path

from core.extract import extract_text
from core.extract.email import extract_email
from core.extract.msgoutlook import extract_msg
from core.scanner import kind_for


class MsgKindTests(unittest.TestCase):
    def test_msg_is_mail(self):
        # one kind for all mail so the UI's Mail chip and type:email cover it
        self.assertEqual(kind_for("C:/mail/report.msg"), "email")

    def test_without_extract_msg_the_text_is_empty_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.msg"
            path.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)
            text = extract_text(path, "email")
            self.assertEqual(text, "")

    def test_with_extract_msg_the_mail_is_read(self):
        # inject a fake extract_msg package: the integration must use it
        fake = types.ModuleType("extract_msg")

        class FakeMessage:
            subject = "Инвентаризация"
            sender = "a@example.com"
            to = ["b@example.com"]
            cc = []
            date = None
            body = "Итоги квартала в приложении."
            headerText = ""

            def close(self):
                pass

        fake.openMsg = lambda _path: FakeMessage()
        saved = {name: sys.modules.get(name) for name in ("extract_msg",)}
        sys.modules["extract_msg"] = fake
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "a.msg"
                path.write_bytes(b"\xd0\xcf\x11\xe0")
                text = extract_email(path)
                self.assertIn("Subject: Инвентаризация", text)
                self.assertIn("Итоги квартала", text)
        finally:
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

    def test_msg_extract_function_bubbles_the_runtime_error(self):
        # no extract_msg anywhere: extract_msg raises, extract_text swallows it
        saved = sys.modules.pop("extract_msg", None)
        try:
            with self.assertRaises(RuntimeError):
                extract_msg("whatever.msg")
        finally:
            if saved is not None:
                sys.modules["extract_msg"] = saved


if __name__ == "__main__":
    unittest.main()
