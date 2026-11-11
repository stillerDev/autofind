"""Paranoid-mode key management and AEAD sealing.

Two primitives, deliberately boring:

- **AES-256-GCM** (`cryptography` package, the same primitive TLS 1.3 and
  Signal use) seals every sensitive blob on disk. A sealed blob is
  ``AFS1 | 12-byte nonce | ciphertext+tag`` - the magic prefix lets readers
  tell sealed and legacy plaintext apart during migration.
- **DPAPI** (crypt32 via ctypes, Windows' own per-account data protection)
  wraps the 32-byte master key into ``<home>/vault.key``. No passphrase to
  forget: the key belongs to the Windows account, so a stolen disk without
  the account is just ciphertext, and another local user cannot unwrap it.

The master key itself never touches disk in the clear. Losing or deleting
``vault.key`` makes every sealed blob unreadable - the index has to be
rebuilt, which the UI states before sealing is enabled.
"""

from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes
from pathlib import Path

MAGIC = b"AFS1"          # sealed blob prefix
KEY_MAGIC = b"AFVK1"     # vault.key prefix
NONCE = 12
KEY_LEN = 32
_ENTROPY = "autofind.vault.v1"
_UI_FORBIDDEN = 0x1


class VaultError(RuntimeError):
    """Raised when sealed data exists but the key is unavailable or wrong."""


# --------------------------------------------------------------------- DPAPI

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> _BLOB:
    buf = ctypes.create_string_buffer(data, len(data))
    return _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def dpapi_protect(data: bytes, entropy: str = _ENTROPY) -> bytes:
    """Wrap bytes with the current Windows account's DPAPI scope."""
    crypt32 = ctypes.windll.crypt32
    in_blob = _blob(data)
    ent_blob = _blob(entropy.encode("utf-8"))
    out = _BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), None, ctypes.byref(ent_blob), None, None,
        _UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise VaultError(f"DPAPI protect failed: {ctypes.GetLastError()}")
    raw = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return raw


def dpapi_unprotect(blob: bytes, entropy: str = _ENTROPY) -> bytes:
    """Unwrap bytes that came from dpapi_protect (same Windows account)."""
    crypt32 = ctypes.windll.crypt32
    in_blob = _blob(blob)
    ent_blob = _blob(entropy.encode("utf-8"))
    out = _BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, ctypes.byref(ent_blob), None, None,
        _UI_FORBIDDEN, ctypes.byref(out),
    )
    if not ok:
        raise VaultError(f"DPAPI unprotect failed: {ctypes.GetLastError()}")
    raw = ctypes.string_at(out.pbData, out.cbData)
    ctypes.windll.kernel32.LocalFree(out.pbData)
    return raw


# ------------------------------------------------------------------- AES-GCM

def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(key)


# --------------------------------------------------------------------- Vault

class Vault:
    """The paranoid-mode key holder: seals and opens content blobs."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_LEN:
            raise VaultError("master key must be 32 bytes")
        self._key = key
        self._aes = _aesgcm(key)

    def seal(self, data: bytes) -> bytes:
        if data.startswith(MAGIC):
            return data  # already sealed (idempotent re-seal of a sealed row)
        nonce = os.urandom(NONCE)
        return MAGIC + nonce + self._aes.encrypt(nonce, data, None)

    def open_bytes(self, blob: bytes) -> bytes:
        if not blob.startswith(MAGIC):
            return blob  # legacy plaintext row, migration tolerance
        nonce = blob[len(MAGIC):len(MAGIC) + NONCE]
        try:
            return self._aes.decrypt(nonce, blob[len(MAGIC) + NONCE:], None)
        except Exception as exc:
            raise VaultError("sealed blob failed to decrypt (wrong key or corrupt data)") from exc

    @staticmethod
    def is_sealed(raw) -> bool:
        if isinstance(raw, memoryview):
            raw = raw.tobytes()
        return isinstance(raw, (bytes, bytearray)) and raw[:4] == MAGIC


# ----------------------------------------------------------------- key file

KEY_FILE = "vault.key"


def key_path(home) -> Path:
    return Path(home) / KEY_FILE


def enable(home) -> Vault:
    """Create (or reuse) the DPAPI-wrapped master key and return the Vault."""
    if os.name != "nt":
        raise VaultError("paranoid mode needs Windows DPAPI for key storage")
    path = key_path(home)
    Path(home).mkdir(parents=True, exist_ok=True)
    if path.exists():
        return load(home)
    key = os.urandom(KEY_LEN)
    wrapped = dpapi_protect(key)
    body = KEY_MAGIC + base64.b64encode(wrapped) + b"\n"
    path.write_bytes(body)
    vault = Vault(key)
    _current = vault
    globals()["_CURRENT"] = vault
    return vault


def load(home) -> Vault:
    """Unwrap the stored key and install the process-wide Vault."""
    path = key_path(home)
    if not path.exists():
        raise VaultError(f"no {KEY_FILE} in {home}: the key was deleted or paranoid mode was never enabled")
    body = path.read_bytes()
    if not body.startswith(KEY_MAGIC):
        raise VaultError(f"{path} is not an AutoFind vault key file")
    key = dpapi_unprotect(base64.b64decode(body[len(KEY_MAGIC):].strip()))
    vault = Vault(key)
    globals()["_CURRENT"] = vault
    return vault


def current() -> "Vault | None":
    """The Vault installed for this process, or None (paranoid mode off)."""
    return globals().get("_CURRENT")


def install(vault: Vault) -> None:
    globals()["_CURRENT"] = vault


def is_enabled(home) -> bool:
    """A vault key exists on disk (sealing may or may not have run yet)."""
    return key_path(home).exists()
