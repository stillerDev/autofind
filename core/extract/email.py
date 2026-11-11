"""Text from `.eml` and mbox mail, using only `email` from the standard library.

A mail is worth indexing for its headers as much as its body: "from", "to" and
the subject are the words people remember, and a body that arrives as HTML is
reduced to the text a human would read.
"""

from __future__ import annotations

import email
import email.policy
import html
import mailbox
import re
from email.message import Message
from pathlib import Path

MAX_MESSAGES = 200
MAX_BODY_BYTES = 500_000

_TAG = re.compile(r"<[^>]+>")
_STYLE = re.compile(r"(?is)<(script|style)\b.*?</\1>")


def html_to_text(markup: str) -> str:
    text = _STYLE.sub(" ", markup or "")
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", text)
    text = _TAG.sub(" ", text)
    return re.sub(r"[ \t]+", " ", html.unescape(text)).strip()


def _headers(message: Message) -> str:
    fields = ("Subject", "From", "To", "Cc", "Date")
    rows = []
    for name in fields:
        value = message.get(name)
        if value:
            rows.append(f"{name}: {value}")
    return "\n".join(rows)


def _decode_bytes(data: bytes, charset: str | None) -> str:
    """Decode a body with the declared charset, then the usual 8-bit suspects.

    Old mbox files carry raw UTF-8 or CP1251 with no `Content-Type` at all; the
    mail policy would read those as ASCII and turn every Cyrillic word into a
    replacement character.
    """
    encodings = [charset] if charset else []
    encodings += ["utf-8", "cp1251", "latin-1"]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("utf-8", "replace")


def _body(message: Message, budget: int) -> str:
    parts: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        try:
            disposition = part.get_content_disposition()
        except AttributeError:
            disposition = None
        if disposition == "attachment":
            name = part.get_filename() or ""
            if Path(name).suffix.lower() in {".txt", ".md", ".csv", ".log"}:
                payload = part.get_payload(decode=True) or b""
                try:
                    parts.append(payload[:budget].decode("utf-8", "replace"))
                except Exception:
                    pass
            continue
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        raw = part.get_payload(decode=True)
        if raw:
            charset = part.get_content_charset()
            content = _decode_bytes(raw, charset)
        else:
            try:
                content = part.get_content()
            except (KeyError, LookupError, UnicodeDecodeError, AttributeError):
                continue
        if not isinstance(content, str):
            continue
        if ctype == "text/html":
            content = html_to_text(content)
        if content.strip():
            parts.append(content.strip())
        if sum(len(item) for item in parts) >= budget:
            break
    return "\n\n".join(parts)[:budget]


def _message_text(message: Message) -> str:
    headers = _headers(message)
    body = _body(message, MAX_BODY_BYTES)
    return "\n".join(part for part in (headers, body) if part)


def extract_eml(path: str | Path, max_bytes: int = MAX_BODY_BYTES) -> str:
    with open(path, "rb") as handle:
        message = email.message_from_binary_file(handle, policy=email.policy.default)
    return _message_text(message)[: int(max_bytes)]


def extract_mbox(path: str | Path, max_bytes: int = MAX_BODY_BYTES) -> str:
    blocks: list[str] = []
    total = 0
    box = mailbox.mbox(str(path))
    try:
        for index, key in enumerate(box.keys()):
            if index >= MAX_MESSAGES or total >= max_bytes:
                break
            try:
                # Re-parse with the modern policy: the mailbox's own message
                # class cannot decode a non-ASCII subject or a quoted-printable
                # body, and a mail that arrives as question marks is not text.
                message = email.message_from_bytes(
                    box.get_bytes(key) or b"", policy=email.policy.default
                )
                text = _message_text(message)
            except Exception:
                continue
            if not text:
                continue
            blocks.append(text)
            total += len(text)
    finally:
        try:
            box.close()
        except Exception:
            pass
    return "\n\n".join(blocks)[: int(max_bytes)]


def extract_email(path: str | Path, max_bytes: int = MAX_BODY_BYTES) -> str:
    if Path(path).suffix.lower() == ".mbox":
        return extract_mbox(path, max_bytes)
    return extract_eml(path, max_bytes)
