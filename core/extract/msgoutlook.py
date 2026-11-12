"""Outlook .msg extraction via the extract-msg package."""

from __future__ import annotations
from pathlib import Path


def extract_msg(path, max_bytes=20 * 1024 * 1024):
    """Read an Outlook .msg file and return Subject + From + body text."""
    try:
        import extract_msg
    except ImportError as exc:
        raise RuntimeError("extract-msg package not installed") from exc
    try:
        msg = extract_msg.openMsg(path)
        parts = []
        subject = str(msg.subject or "")
        sender = str(msg.sender or "")
        if subject:
            parts.append(f"Subject: {subject}")
        if sender:
            parts.append(f"From: {sender}")
        body = str(msg.body or "")
        if body:
            parts.append(body[:max_bytes])
        msg.close()
        return chr(10).join(parts)
    except RuntimeError:
        raise
    except Exception:
        return ""
