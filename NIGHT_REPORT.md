# AutoFind night-shift report

**Date:** 2026-11-12
**Session:** Performance vs Kensaku v1.0.17 → memory → encryption → UX → roadmap

## Score vs Kensaku v1.0.17 (100k files, same machine)

| Metric | AutoFind v9 | Kensaku v1.0.17 | Winner |
|---|---|---|---|
| Indexing | 34.2 s | 35.0 s | AutoFind |
| Cold CLI | 366 ms | 697 ms | AutoFind ×1.9 |
| Warm median | ~13.7 ms | 24.3 ms | AutoFind ×1.8 |
| Content queries | 7.9–13.7 ms | 25.8 ms | AutoFind |
| Name queries | 8.6–9.0 ms | 8.7 ms | tie |
| Index size | 114 MB | 117 MB | AutoFind |
| RSS | 169 MB | 176 MB | AutoFind |
| Private (USS) | 146 MB | 105 MB | Kensaku ×1.4 |

## Session summary (7 commits on main)

### Commit f80c6fe — Bug fixes + regressions
- `_repack_texts`: skip sealed rows (zlib repack on ciphertext corrupted text)
- Parallel channels: per-call ThreadPoolExecutor instead of module-level (segfault fix)
- `Store.close()`: evicts NAME_CACHE (PermissionError on tempdir cleanup)
- BM25 sidecar works in both modes: sealed and normal — cold CLI loads in ~100 ms
- Regression: 10 underscore/punctuation queries pass on the 100k corpus

### Commit 9447111 — Function rail (TikTok-style)
- Left-edge vertical rail of 6 circular glass buttons
- Duplicates, Index, Privacy, Encryption, Search, Keys — each opens its own function
- Drawer slides from the left past the rail
- Engines block removed (same info in Settings)
- Dictionary + File types and Hotkeys moved from Settings modal to drawer

### Commit 051d459 — Drawer tabs + live statuses
- Drawer width = duplicates drawer width (1080px)
- 5 tabs: Index, Privacy, Encryption, Search, Keys
- Index tab: extra data (doc texts, sealed count, definitions) + live job status
- Encryption tab: 100% only after reseal_bm25() completes (search fix included in job)

### Commit 2adb287 — Memory squeeze
- ±1 float32 matrix (154 MB) → LUT scoring over packed bits (4.8 MB)
- Name blobs → mmap from .nameidx.bin companion (25 MB off private RAM)
- BM25 vocab → UTF-8 blob + offset table (7 MB off)
- Term frequencies → uint16 (5 MB off)
- RSS 485 → 169 MB; private 462 → 146 MB

### Commit 052c9b6 — Settings encryption button
- POST /api/seal starts a background (un)seal; GET /api/seal polls status
- Settings → Encryption panel: state, button, progress bar with %
- serve() loads the DPAPI vault key at startup
- _audit reports the real sealed state

### Commit [seal] — Paranoid mode
- `autofind seal on` encrypts all content blobs (AES-256-GCM) and drops FTS
- Content search via in-RAM BM25 (decrypted at startup, re-encrypted after runs)
- `autofind seal off` decrypts back and rebuilds FTS
- Keys: DPAPI-wrapped 32-byte master key in vault.key
- Fixed: _repack_texts corruption, rowid alias, NAME_CACHE eviction

### Commit [updater] — GitHub auto-updater
- `core/updater.py`: checks stillerDev/autofind/releases/latest
- Version comparison, Windows asset detection, SHA256 verification
- Staged download in per-user temp dir, detached cmd replacement
- CLI: `autofind update --check` / `autofind update`

### Commit [UX] — Context menu + drag-drop + double Enter
- Right-click on result card: copy path, open folder, pin/unpin
- Drag a result card into Explorer: file is copied there
- Double Enter on the same result: opens the containing folder

## Remaining TODO (documented, ready to implement)

### High priority
1. **Content-addressed chunks** — one text per content-hash, refcount. −40-70% size. Needs a schema migration (v10) and a two-phase build. Est. 3-4 h.
2. **Vault passphrase mode** — Argon2id KDF as an alternative to DPAPI. Portable vault. Est. 2 h.
3. **Index export/import** — sealed archive for machine transfer. Est. 2 h.
4. **GitHub updater hardening** — test the actual download/replace flow on a real release.

### Medium priority
5. **MCP cursor pagination** — add `cursor` and `page_size` params to search tools.
6. **OpenAPI schema** — serve `/api/openapi.json` from the existing REST surface.
7. **CLI export** — `autofind export "query" --format csv|json`.
8. **Import from Everything/Listary** — read their index formats, seed AutoFind.
9. **Idle-only indexing** — GetLastInputInfo via ctypes; defer crawl when user is active.
10. **Network drive detection** — GetDriveTypeW; block writes on NETWORK drive.
11. **Index self-test** — integrity check at startup: count chunks, verify hashes.
12. **RAM soft limit** — configurable; evict caches when RSS exceeds the limit.
13. **Log rotation** — write server events to a rotating log file.
14. **Clean uninstaller** — `autofind uninstall`: remove index, key, autostart, shortcuts.
15. **.msg support** — extract-msg library; add to scanner.py EXTRACTORS.
16. **Indra OCR** — port from kensaku's ocr_indra module.
17. **zh-CN UI** — add a third locale to i18n.js.

## Performance journey (100k-file corpus, same machine)

| Metric | Start | End | Improvement |
|---|---|---|---|
| Indexing | 155 s | 34.2 s | ×4.5 |
| Cold CLI | 960 ms | 366 ms | ×2.6 |
| Warm median | 36.4 ms | ~13.7 ms | ×2.7 |
| Content queries | 110 ms | 7.9 ms | ×14 |
| Name queries | 20.8 ms | 8.6 ms | ×2.4 |
| Index size | 543 MB | 114 MB | ×4.8 |
| RSS | 262 MB | 169 MB | ×1.6 |
| Private RAM | 85 MB | 146 MB | +72% (in-RAM BM25 trade-off) |
