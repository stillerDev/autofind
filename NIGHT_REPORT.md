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
13. **Log rotation** — write server events to a rotating log file. **DONE** — `core/logger.py` (size-rotated `~/.autofind/autofind.log`), wired into index jobs, watcher errors, boot; readable via `GET /api/log`.
14. **Clean uninstaller** — `autofind uninstall`: remove index, key, autostart, shortcuts. **DONE** — dry run by default, `--yes` to actually delete.
15. **.msg support** — extract-msg library; add to scanner.py EXTRACTORS. **DONE** — `.msg` joins kind "email"; `core/extract/msgoutlook.py` uses the optional `extract-msg` package (requirements.txt), without it the file is indexed without text.
16. **Indra OCR** — port from kensaku's ocr_indra module. **DONE** — `core/ocr/indra.py` is the native Rust port (imageops, CTC text with Hangul/RTL handling, XY-cut reading order, deskew, the det→route→rec pipeline) translated to numpy + onnxruntime, no new dependencies. The 27 MB model pack downloads on demand (`autofind ocr download indra`) with pinned SHA-256s; the weights are under a modified OpenRAIL-M license, so they are never bundled. Indra leads the fallback tier and the CJK list, reads RTL and vertical CJK, and answers `--engine indra`; 33 ported unit tests in `tests/test_ocr_indra.py` assert the same pixel values as the Rust tests.
17. **zh-CN UI** — add a third locale to i18n.js. **DONE** — all 455 keys translated, `checkTranslations()` clean, `locale()` in app.js maps zh→zh-CN for date formatting.
18. **Kensaku parity sweep** — four gaps closed, all uncommitted:
    - **Scanned-PDF OCR** — `core/extract/pdf.py`: a document whose whole text layer is under 50 chars is pages of pixels; pages render via PyMuPDF (50 MP cap, 0.01 zoom floor) and run through the screenshot OCR chain. Kensaku `pdf.rs` policy ported: per-engine page caps (windows 20 / indra 10 / default 6 — a timed-out extraction retried by the watcher must terminate), the `\n... [content truncated at limit]` budget note, and the `[Scanned PDF: image only]` marker that keeps the file indexed when OCR is off or unavailable. 8 tests with real scanned-PDF fixtures and real OCR (`tests/test_pdf_scanned.py`).
    - **CSV/TSV rows** — `core/extract/delimited.py`: `pandas.head` shape, `column = "value"` per line so a cell-value search lands on its row; delimiter sniffed over the first 20 records (hint first), 200-row head, byte cap drops the half record, UTF-16 by BOM, NUL-without-BOM rejected as binary. The kind stays `text` (no new store/query-facet surface). 13 tests (`tests/test_delimited.py`).
    - **Maintenance** — `core/maintenance.py` (Kensaku `maintenance.rs` port): `autofind maintenance` prints an inventory (index, packs, import backups, `.part` files, updater temp dirs, logs); `--clean` removes what no live process can still be using (newest `import-backup/` survives; `.part` older than 1 h; `autofind-update-*` temp dirs of dead PIDs, current PID's stage and anything < 24 h old kept). `autofind reset [--yes] [--with-models]` wipes user state but keeps the model packs by default — a reset must not cost a 55 MB re-download. The same cleanup pass runs quietly at the start of every `run_index`. 10 tests (`tests/test_maintenance.py`).
    - **Model downloader** — `core/models.py`: `autofind model download e5` installs the int8 multilingual-e5-small export into `~/.autofind/models/e5-small/` (118 MB model.onnx + 17 MB tokenizer.json), pinned by byte length and SHA-256 at a fixed HF commit, huggingface with hf-mirror fallback, `.part` staging renamed only after verification — the same guarantees as the Indra pack; interrupted runs resume. `autofind model status` names the exact problem (missing / size / checksum). Pins verified against the live repo by HEAD. `get_embedder` now passes `models/e5-small/` to `OnnxE5`, matching the documented drop-in path. 6 tests with a fake network (`tests/test_models.py`).

    Full suite: 527 passed. Still open from the sweep: parquet extractor (optional dep), crashlog channel, opt-in external extractor plugins.

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
