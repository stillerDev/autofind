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
- **Passphrase mode** is the portable alternative: the same master key is
  wrapped with AES-256-GCM under a key derived from a passphrase (Argon2id
  when `argon2-cffi` is installed, stdlib scrypt otherwise). The vault file
  records which KDF and parameters produced the wrapping key, so the file
  travels between machines and operating systems with its owner.

The master key itself never touches disk in the clear. Losing or deleting
``vault.key`` makes every sealed blob unreadable - the index has to be
rebuilt, which the UI states before sealing is enabled. In passphrase mode
the same is true of forgetting the passphrase: there is no backdoor, that
is the point.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
from ctypes import wintypes
from pathlib import Path

MAGIC = b"AFS1"          # sealed blob prefix
KEY_MAGIC = b"AFVK1"     # vault.key prefix (DPAPI)
KEY_MAGIC_V2 = b"AFVK2"  # vault.key prefix (passphrase, portable)
NONCE = 12
KEY_LEN = 32
_ENTROPY = "autofind.vault.v1"
_UI_FORBIDDEN = 0x1

# Passphrase-mode KDF parameters. Deliberately heavier than OWASP's Argon2id
# baseline because a passphrase unlock happens once per process, not per row.
# scrypt cost = 128 * n * r = 128 MiB here (not 256 MiB), a few seconds.
SCRYPT_PARAMS = {"n": 2 ** 17, "r": 8, "p": 1}   # 128 MiB, a few seconds
ARGON2_PARAMS = {"time_cost": 4, "memory_cost": 262144, "parallelism": 2}  # 256 MiB

# The passphrase can arrive through the environment for headless runs (the
# desktop window cannot ask at boot; the CLI can also prompt).
PASSPHRASE_ENV = "AUTOFIND_PASSPHRASE"


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


# ----------------------------------------------------------------- KDFs

def argon2_available() -> bool:
    """Argon2id runs only when `argon2-cffi` is installed; scrypt needs nothing."""
    try:
        import argon2.low_level  # noqa: F401
        return True
    except ImportError:
        return False


def _derive(passphrase: str, salt: bytes, kdf: str, params: dict) -> bytes:
    """Stretch a passphrase into the 32-byte key that wraps the master key."""
    data = passphrase.encode("utf-8")
    if kdf == "argon2id":
        try:
            from argon2.low_level import Type, hash_secret_raw
        except ImportError as exc:
            raise VaultError(
                "this vault file was created with Argon2id but the package is "
                "not installed here. Run: pip install argon2-cffi"
            ) from exc
        return hash_secret_raw(
            data, salt,
            time_cost=int(params["time_cost"]),
            memory_cost=int(params["memory_cost"]),
            parallelism=int(params["parallelism"]),
            hash_len=KEY_LEN, type=Type.ID,
        )
    if kdf == "scrypt":
        return hashlib.scrypt(
            data, salt=salt,
            n=int(params["n"]), r=int(params["r"]), p=int(params["p"]),
            maxmem=2 ** 31 - 1, dklen=KEY_LEN,
        )
    raise VaultError(f"unknown KDF {kdf!r} in the vault file")


def _pick_kdf() -> str:
    return "argon2id" if argon2_available() else "scrypt"


# --------------------------------------------------------------------- Vault

class Vault:
    """The paranoid-mode key holder: seals and opens content blobs."""

    def __init__(self, key: bytes) -> None:
        if len(key) != KEY_LEN:
            raise VaultError("master key must be 32 bytes")
        self._key = key
        self._aes = _aesgcm(key)

    def seal(self, data: bytes, *, already_sealed: bool = False) -> bytes:
        """Seal `data` into ``AFS1 | nonce | ciphertext+tag``.

        ``already_sealed=True`` is the explicit idempotent path for a caller
        that *knows* the row was sealed by this vault. The prefix alone is not
        trusted: arbitrary plaintext beginning with ``AFS1`` used to sail
        through in paranoid mode. Genuine blobs are recognised by verifying
        the AEAD tag with this master key, so the common re-seal path stays
        idempotent without leaving a plaintext hole.
        """
        if already_sealed:
            return data
        if isinstance(data, (bytes, bytearray, memoryview)) and self._decryptable(data):
            return data  # a real blob from this vault — idempotent re-seal
        nonce = os.urandom(NONCE)
        return MAGIC + nonce + self._aes.encrypt(nonce, bytes(data), None)

    def _decryptable(self, blob) -> bool:
        """True only for an ``AFS1`` blob whose AEAD tag verifies with our key."""
        head = len(MAGIC) + NONCE
        view = memoryview(blob)
        if len(view) <= head + 16:  # AES-GCM tag is 16 bytes
            return False
        if bytes(view[:len(MAGIC)]) != MAGIC:
            return False
        nonce = bytes(view[len(MAGIC):head])
        try:
            self._aes.decrypt(nonce, view[head:], None)
        except Exception:
            return False
        return True

    def open_bytes(self, blob) -> bytes:
        # `blob` may be a memoryview (e.g. a slice of the mmapped sidecar):
        # slicing an mmap as bytes copies the whole blob, while a memoryview
        # slice is free — worth it for the ~30 MB sealed BM25 header.
        if isinstance(blob, memoryview):
            if bytes(blob[:len(MAGIC)]) != MAGIC:
                return blob.tobytes()  # legacy plaintext row
            mv = blob
        else:
            if not blob.startswith(MAGIC):
                return blob  # legacy plaintext row, migration tolerance
            mv = memoryview(blob)
        nonce = bytes(mv[len(MAGIC):len(MAGIC) + NONCE])
        try:
            return self._aes.decrypt(nonce, mv[len(MAGIC) + NONCE:], None)
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
        raise VaultError("DPAPI mode needs Windows; on this platform use passphrase mode")
    path = key_path(home)
    Path(home).mkdir(parents=True, exist_ok=True)
    if path.exists():
        return load(home)
    key = os.urandom(KEY_LEN)
    wrapped = dpapi_protect(key)
    body = KEY_MAGIC + base64.b64encode(wrapped) + b"\n"
    path.write_bytes(body)
    vault = Vault(key)
    globals()["_CURRENT"] = vault
    return vault


def key_mode(home) -> str:
    """How the stored key is wrapped: "dpapi", "passphrase" or "" (absent)."""
    path = key_path(home)
    if not path.exists():
        return ""
    head = path.read_bytes()[:6]
    if head.startswith(KEY_MAGIC):
        return "dpapi"
    if head.startswith(KEY_MAGIC_V2):
        return "passphrase"
    return "unknown"


def enable_passphrase(home, passphrase: str) -> Vault:
    """Create the passphrase-wrapped master key and return the Vault.

    The portable counterpart to `enable`: the same key file opens on any
    machine, because the wrapping key is derived from the passphrase alone
    (Argon2id when available, stdlib scrypt otherwise) — nothing on disk
    depends on the Windows account. An empty or short passphrase is refused
    here rather than stored badly.
    """
    passphrase = str(passphrase or "")
    if len(passphrase) < 8:
        raise VaultError("the passphrase must be at least 8 characters")
    path = key_path(home)
    Path(home).mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise VaultError(
            f"{KEY_FILE} already exists; delete it first to switch mode "
            "(a deleted key makes previously sealed content unreadable)"
        )
    key = os.urandom(KEY_LEN)
    salt = os.urandom(16)
    kdf = _pick_kdf()
    params = ARGON2_PARAMS if kdf == "argon2id" else SCRYPT_PARAMS
    header = {
        "kdf": kdf,
        "salt": base64.b64encode(salt).decode("ascii"),
        "params": params,
    }
    wrapping = _derive(passphrase, salt, kdf, params)
    wrapped = Vault(wrapping).seal(key)
    body = (
        KEY_MAGIC_V2
        # Compact JSON: the header and the base64 payload are split on a
        # space, so the header itself must not contain one.
        + json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        + b" " + base64.b64encode(wrapped) + b"\n"
    )
    path.write_bytes(body)
    vault = Vault(key)
    globals()["_CURRENT"] = vault
    return vault


def load(home, passphrase: str | None = None) -> Vault:
    """Unwrap the stored key and install the process-wide Vault.

    A DPAPI file needs no passphrase (the Windows account is the secret); a
    passphrase file refuses to open without the right passphrase — either
    given here or read from the `AUTOFIND_PASSPHRASE` environment variable
    for headless runs.
    """
    path = key_path(home)
    if not path.exists():
        raise VaultError(f"no {KEY_FILE} in {home}: the key was deleted or paranoid mode was never enabled")
    body = path.read_bytes()
    if body.startswith(KEY_MAGIC):
        key = dpapi_unprotect(base64.b64decode(body[len(KEY_MAGIC):].strip()))
    elif body.startswith(KEY_MAGIC_V2):
        head, _, payload = body[len(KEY_MAGIC_V2):].strip().partition(b" ")
        try:
            header = json.loads(head.decode("utf-8"))
            salt = base64.b64decode(header["salt"])
            kdf, params = str(header["kdf"]), dict(header["params"])
        except (ValueError, KeyError, TypeError) as exc:
            raise VaultError(f"{path} has a damaged passphrase header") from exc
        if passphrase is None:
            passphrase = os.environ.get(PASSPHRASE_ENV) or ""
        if not passphrase:
            raise VaultError(
                "this vault opens with a passphrase; pass it on the command "
                f"or set {PASSPHRASE_ENV}"
            )
        wrapping = _derive(passphrase, salt, kdf, params)
        key = Vault(wrapping).open_bytes(base64.b64decode(payload))
    else:
        raise VaultError(f"{path} is not an AutoFind vault key file")
    vault = Vault(key)
    globals()["_CURRENT"] = vault
    return vault


def try_autoload(home) -> "Vault | None":
    """Load the vault at startup when it can be done without a human.

    DPAPI unwraps silently on its own account. A passphrase file is tried
    against the environment variable only — a desktop window has nowhere to
    ask, and search will surface the passphrase error with instructions.
    """
    if key_mode(home) == "passphrase" and PASSPHRASE_ENV not in os.environ:
        return None  # expected: nobody typed a passphrase yet
    try:
        return load(home)
    except VaultError:
        return None  # not sealed, or the key is gone - search surfaces it


def current() -> "Vault | None":
    """The Vault installed for this process, or None (paranoid mode off)."""
    return globals().get("_CURRENT")


def install(vault: Vault) -> None:
    globals()["_CURRENT"] = vault


def is_enabled(home) -> bool:
    """A vault key exists on disk (sealing may or may not have run yet)."""
    return key_path(home).exists()
