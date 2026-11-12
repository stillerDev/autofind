"""Paranoid mode tests: vault crypto, seal/unseal migration, search parity."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core import vault as vault_mod
from core.config import IndexConfig
from core.embed.hashing import HashingEmbedder
from core.indexer import run_index
from core.search import search_page
from core.store import Store


def _make_screenshots(root: Path, count: int = 4) -> None:
    from tests.test_core import make_screenshot

    root.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        make_screenshot(root / f"shot-{index}.png", f"тест прокси {index}", dark=(index % 2 == 0))


class VaultTests(unittest.TestCase):
    def test_seal_open_roundtrip(self):
        v = vault_mod.Vault(b"" * 32)
        blob = v.seal(b"secret payload" * 50)
        self.assertTrue(blob.startswith(b"AFS1"))
        self.assertEqual(v.open_bytes(blob), b"secret payload" * 50)
        self.assertNotIn(b"secret payload", blob)

    def test_seal_is_idempotent(self):
        v = vault_mod.Vault(b"" * 32)
        blob = v.seal(b"data")
        self.assertEqual(v.seal(blob), blob)

    def test_unseal_passthrough_for_legacy(self):
        v = vault_mod.Vault(b"" * 32)
        self.assertEqual(v.open_bytes(b"legacy plaintext"), b"legacy plaintext")

    def test_sealed_detects_prefix(self):
        self.assertTrue(vault_mod.Vault.is_sealed(b"AFS1whatever"))
        self.assertFalse(vault_mod.Vault.is_sealed(b"plain"))
        self.assertFalse(vault_mod.Vault.is_sealed(None))

    def test_wrong_key_raises_clear_error(self):
        v1 = vault_mod.Vault(b"" * 32)
        blob = v1.seal(b"payload")
        v2 = vault_mod.Vault(b"" * 32)
        with self.assertRaises(vault_mod.VaultError):
            v2.open_bytes(blob)


class VaultKeyFileTests(unittest.TestCase):
    def test_enable_load_roundtrip(self):
        if __import__("os").name != "nt":
            self.skipTest("DPAPI is Windows-only")
        with tempfile.TemporaryDirectory() as home:
            v1 = vault_mod.enable(home)
            v2 = vault_mod.load(home)
            blob = v1.seal(b"cross-process payload")
            self.assertEqual(v2.open_bytes(blob), b"cross-process payload")
            self.assertTrue(vault_mod.is_enabled(home))
            self.assertTrue(vault_mod.key_path(home).exists())


class PassphraseVaultTests(unittest.TestCase):
    """Passphrase mode: the portable key file, sealed and opened without DPAPI."""

    PASS = "correct horse battery"

    def test_roundtrip_and_kdf_recorded(self):
        with tempfile.TemporaryDirectory() as home:
            v1 = vault_mod.enable_passphrase(home, self.PASS)
            self.assertEqual(vault_mod.key_mode(home), "passphrase")
            body = vault_mod.key_path(home).read_bytes()
            self.assertTrue(body.startswith(vault_mod.KEY_MAGIC_V2))
            # The header names the KDF and its parameters, so the file opens
            # on a machine that has never seen these exact settings.
            import json
            header = json.loads(body[len(vault_mod.KEY_MAGIC_V2):].split(b" ", 1)[0])
            self.assertIn(header["kdf"], ("argon2id", "scrypt"))
            self.assertTrue(header["params"])
            v2 = vault_mod.load(home, self.PASS)
            blob = v1.seal(b"portable payload")
            self.assertEqual(v2.open_bytes(blob), b"portable payload")

    def test_env_var_unlocks(self):
        with tempfile.TemporaryDirectory() as home:
            vault_mod.enable_passphrase(home, self.PASS)
            old = vault_mod.PASSPHRASE_ENV
            __import__("os").environ[old] = self.PASS
            try:
                v = vault_mod.load(home)  # no explicit passphrase
                self.assertEqual(v.open_bytes(v.seal(b"x")), b"x")
            finally:
                del __import__("os").environ[old]

    def test_wrong_passphrase_raises(self):
        with tempfile.TemporaryDirectory() as home:
            vault_mod.enable_passphrase(home, self.PASS)
            with self.assertRaises(vault_mod.VaultError):
                vault_mod.load(home, "wrong passphrase entirely")

    def test_missing_passphrase_tells_where_to_put_it(self):
        with tempfile.TemporaryDirectory() as home:
            vault_mod.enable_passphrase(home, self.PASS)
            with self.assertRaises(vault_mod.VaultError) as caught:
                vault_mod.load(home)
            self.assertIn(vault_mod.PASSPHRASE_ENV, str(caught.exception))

    def test_short_passphrase_refused(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(vault_mod.VaultError):
                vault_mod.enable_passphrase(home, "short")

    def test_existing_key_file_not_silently_overwritten(self):
        if __import__("os").name != "nt":
            self.skipTest("DPAPI is Windows-only")
        with tempfile.TemporaryDirectory() as home:
            vault_mod.enable(home)
            with self.assertRaises(vault_mod.VaultError):
                vault_mod.enable_passphrase(home, self.PASS)
            self.assertEqual(vault_mod.key_mode(home), "dpapi")

    def test_try_autoload_stays_silent_without_env(self):
        with tempfile.TemporaryDirectory() as home:
            vault_mod.enable_passphrase(home, self.PASS)
            vault_mod.install(None)  # a fresh process would have no vault
            old = vault_mod.PASSPHRASE_ENV
            __import__("os").environ.pop(old, None)
            try:
                self.assertIsNone(vault_mod.try_autoload(home))
                self.assertIsNone(vault_mod.current())
            finally:
                __import__("os").environ.pop(old, None)


class SealMigrationTests(unittest.TestCase):
    """Full cycle on a real (small) index: seal, search parity, unseal."""

    def _index(self, tmp: str):
        root = Path(tmp) / "corpus"
        root.mkdir(parents=True, exist_ok=True)
        (root / "notes-1.md").write_text(
            "поиск по документам и файлам: бюджетный отчёт за квартал", encoding="utf-8"
        )
        (root / "notes-2.md").write_text(
            "второй документ про квартальный бюджет и план", encoding="utf-8"
        )
        config = IndexConfig(home=Path(tmp) / "home")
        embedder = HashingEmbedder()
        store = Store(Path(tmp) / "index.db", embedder.name, embedder.dim)
        run_index([root], config, store, embedder)
        return config, store

    def test_seal_unseal_cycle(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, store = self._index(tmp)
            db_path = Path(tmp) / "index.db"
            emb = HashingEmbedder()

            before = search_page(store, emb, "бюджет", limit=5, usage=store.usage_map())
            self.assertTrue(before.hits)
            before_paths = [h.path for h in before.hits]

            # seal on the writable store, then close: the main db file only
            # reflects the seal after the WAL is checkpointed, and an open
            # connection holds the pre-seal snapshot.
            vault = vault_mod.enable(Path(tmp) / "home")
            store.seal_index()
            self.assertTrue(store.sealed)
            store.close()

            raw = db_path.read_bytes()
            self.assertNotIn("бюджетный".encode("utf-8"), raw)
            self.assertNotIn("квартальный".encode("utf-8"), raw)

            # search parity through the sealed BM25 sidecar (read-only store)
            store = Store(db_path, emb.name, emb.dim, readonly=True)
            after = search_page(store, emb, "бюджет", limit=5, usage=store.usage_map())
            self.assertTrue(after.hits)
            self.assertEqual(before_paths, [h.path for h in after.hits])
            self.assertIn("бюджет", store.file_text(after.hits[0].file_id).lower())
            store.close()

            # unseal restores plaintext and FTS (writable store)
            store = Store(db_path, emb.name, emb.dim, readonly=False)
            store.unseal_index()
            self.assertFalse(store.sealed)
            self.assertIn("бюджет", store.file_text(after.hits[0].file_id).lower())
            row = store.conn.execute(
                "SELECT name FROM sqlite_master WHERE name = 'chunks_fts'"
            ).fetchone()
            self.assertIsNotNone(row)
            fts_rows = store.conn.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0]
            self.assertEqual(fts_rows, 2)  # postings rebuilt from decrypted text
            store.close()
            vault_mod.install(None)
            # NOTE: file_texts.data stays zlib-compressed by design - the raw
            # file never contained the plaintext in either state.

    def test_sealed_without_key_raises_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            config, store = self._index(tmp)
            db_path = Path(tmp) / "index.db"
            vault_mod.enable(Path(tmp) / "home")
            store.seal_index()
            # simulate a lost key: the process vault is gone and the key file too
            vault_mod.install(None)
            vault_mod.key_path(Path(tmp) / "home").unlink()

            digest = store.conn.execute("SELECT hash FROM file_texts").fetchone()[0]
            with self.assertRaises(vault_mod.VaultError):
                store.text_of(digest)

            store.close()
            vault_mod.install(None)


if __name__ == "__main__":
    unittest.main()
