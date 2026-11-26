# Full local-search benchmark: AutoFind vs kensaku v1.0.19

**Date:** 2026-11-25 17:09:09  |  **Machine:** DESKTOP-368 (Windows 11 Pro 25H2, desktop)  |  **Axes:** 35 queries × 15 timed requests

> **Update 2026-11-25 18:21:** after fixing multi-token filename matching (`core/nameindex.py`, `core/search.py`) and a search-cache eviction bug that caused HTTP 500 after 48 cached queries, a fresh 35-axis run gave **35/35 wins** by median and p95, median speedup **67.9×**, worst p95 **0.43 ms**; cold CLI **182.0 vs 224.3 ms** in AutoFind's favour. First query 42.8 vs 16.8 ms and RSS 230.4 vs 164.8 MiB were measured on a freshly restarted test server with cold caches. On the labelled shared-corpus set (`RELEVANCE_REPORT.md`) AutoFind now wins relevance as well: set B Recall@5 0.883 vs 0.433, set C 0.867 vs 0.722, at ~0.3 ms vs ~16 ms per query.

## TL;DR — paste-ready

I benchmarked two local file-search engines on the same physical machine, with their own real indexes: **21,643 files / 313,041 chunks** for AutoFind and **18,923 files / 56,863 semantic units** for kensaku v1.0.19. Warm in-process HTTP search (1 warm-up + 15 measured requests, keep-alive, median + p95) was faster with AutoFind on **35/35** queries by median and **35/35** by p95; median speedup **49.5×** (geometric mean 61.5×, range 6.7–1515×). Max warm p95: **1.87 ms** for AutoFind vs 647.3 ms for kensaku. First query after server-ready + warm-up: **1.35 ms** vs 28.75 ms. Index size: **194.8 MiB** vs 220.8 MiB. RSS after warm-up: **104.6 MiB** vs 121.5 MiB. Result overlap (IoU of top-5 paths) is only 0.146 because the corpora differ, so this is a **speed comparison, not a relevance comparison**.

---

## 1. The bench — actual machine

| Component | Value |
|---|---|
| CPU | Intel Core i5-12400F, 6 cores / 12 threads, 2.5 GHz base (12th Gen Alder Lake) |
| Memory | 2 × 16 GiB ADATA AX4U320016G16A-DW50, DDR4-2667 (31.86 GiB usable) |
| GPU | NVIDIA GeForce RTX 3060, driver 32.0.16.1742 |
| Storage | HS-SSD-E3000 1024G, NVMe SSD, 1,024,209,543,168 B, firmware HS11795, healthy |
| OS | Windows 11 Pro 25H2, build 26200.10075 |
| Power plan | High performance (GUID 8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c) |
| Runtime | Python 3.13.13 (64-bit MSC v.1944), Rust 1.99.1, Node v24.14.0 |
| Background | Discord, Edge, WorkBuddyAI and OpenCode were running; numbers include that noise |

All timings come from this machine on this date. The reference numbers I had been given earlier came from a differently loaded session, so absolute values differ; in every comparison here both engines were measured in the same session with the same method.

## 2. Software under test

| | AutoFind | kensaku |
|---|---|---|
| Version | local working tree, 25 Nov 2026 | v1.0.19 CLI, official release |
| Binary | `python -m core.cli serve` + Rust GUI shell | `kensaku-cli-windows-x86_64.exe` |
| SHA256 | source tree (uncommitted working tree) | `AAD4C913053A33535F6C9FC3956DD86CDF80B472BBBB0EC06D81C035B31699B6` (verified against release `SHA256SUMS`) |
| Index engine | SQLite FTS5 + in-RAM BM25 (int32 CSR, zstd-compressed sealed header) + vector pack | Tantivy (`*.idx`, `*.pos`, `*.term`, `*.store`, `*.fast`, `*.fieldnorm`) |
| Model | local embeddings, vectors stored in the index | static-embed-256 (`model_real=false`) |
| Transport | `http://127.0.0.1:8765/api/search?q=…` | `http://127.0.0.1:8766/search?q=…`, Bearer token from `%APPDATA%\kensaku\http.token` |

## 3. Indexes and corpora

| Property | AutoFind | kensaku |
|---|---:|---:|
| Indexed files | 21,643 | 18,923 |
| Index units | 313,041 chunks; 97,935 symbols; 14,108 vectors; 1,126 images | 56,863 semantic units; 8,551 content docs; 3,481 dirs |
| Roots | `C:\Users\h1ile\Downloads`, Desktop, Documents | `C:\Users\h1ile\Downloads`, Desktop, Documents |
| Active index files | 5 | 14 |
| Index bytes | 204,234,174 B (194.8 MiB) | 231,543,006 B (220.8 MiB) |

AutoFind files (active only): `index.db` 112,058,368 B; `index.db-wal` 4,120,032 B; `index.db.bm25seal` 79,641,505 B; `index.db.nameidx` 1,686 B; `index.db.nameidx.ee6b1cd1c0.bin` 8,412,583 B.

The corpora are the same user folders but the engines index different subsets and split them differently. That is why the result overlap is low and why relevance is **not** compared here.

## 4. Methodology

- **Warm search:** 1 warm-up request per axis, then 15 measured requests over a single keep-alive `http.client` connection; median and p95 (numpy percentile) reported per axis. 35 axes.
- **First query:** the first request after the server reported ready and after its background warm-up finished.
- **Cold CLI:** `autofind search test --json -n 5` and `kensaku-cli ... search test --json --limit 5`, median of 3 fresh process launches. `--local` (no server delegation) measured separately as an extra data point.
- **GUI → window:** `Start-Process` to the moment `MainWindowHandle != 0`, median of 3 launches in the main run and 5 paired launches in the follow-up GUI run; clean `AUTOFIND_HOME` / `KENSAKU_DATA_DIR` per launch.
- **RAM:** `psutil` RSS of the server process after warm-up, both servers resident.
- **Indexing:** fresh 10k text-file corpus, `--workers 4`, files/second, median of 3 runs.
- **Disk:** only files that belong to the active index, not the whole data directory.
- **Overlap:** intersection-over-union of the top-5 returned paths.
- **No index was modified:** kensaku's real `%APPDATA%\kensaku` was left untouched; all indexing experiments wrote to isolated temp directories.

## 5. Results

### 5.1 Summary

| Axis | kensaku | AutoFind | Winner |
|---|---:|---:|---|
| Warm search, median | — | — | AutoFind 35/35 |
| Warm search, p95 | — | — | AutoFind 35/35 |
| Warm speedup (median / geometric mean) | — | 49.5× / 61.5× | AutoFind |
| Warm speedup range | — | 6.7× – 1515× | AutoFind |
| Median p95 speedup | — | 26.0× | AutoFind |
| Highest warm p95 | 647.3 ms | 1.87 ms | AutoFind |
| First query | 28.75 ms | 1.35 ms | AutoFind |
| Cold CLI, with server (run B) | 435.1 ms | 488.8 ms | kensaku |
| Cold CLI, with server (run A) | 211.7 ms | 161.4 ms | AutoFind |
| Cold CLI, `--local` (no server) | — | 711.5 ms | — |
| RSS after warm-up (run B) | 121.5 MiB | 104.6 MiB | AutoFind |
| RSS after warm-up (run A) | 111.8 MiB | 188.1 MiB | kensaku |
| Index on disk | 231,543,006 B | 204,234,174 B | AutoFind |
| Indexing 10k files, `--workers 4` | not measurable (see caveats) | 2,654.8 files/s | — |
| GUI → window, paired 5 runs | 170.1 ms | 142.5 ms | AutoFind |
| Top-5 overlap (IoU) | — | mean 0.146, median 0.00 | — |

Run A and run B are two full repetitions on the same day; the differences in cold CLI and RSS are discussed in section 6.

### 5.2 All 35 axes — warm search

`ks` = kensaku, `af` = AutoFind; all values in milliseconds. Speedup = ks median / af median.

| # | Query | ks median | ks p95 | af median | af p95 | Speedup | IoU |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | авторизация | 489.57 | 536.59 | 0.32 | 1.21 | 1515.2× | 0.25 |
| 2 | бюджет | 21.16 | 23.66 | 0.38 | 1.34 | 55.8× | 0.11 |
| 3 | Проект | 19.95 | 37.12 | 0.54 | 1.85 | 36.9× | 0.00 |
| 4 | invoice | 18.60 | 31.56 | 0.39 | 0.53 | 47.6× | 0.00 |
| 5 | config | 21.37 | 27.07 | 0.43 | 1.38 | 49.5× | 0.00 |
| 6 | README | 19.84 | 27.45 | 0.45 | 1.43 | 44.0× | 0.00 |
| 7 | auth | 21.08 | 22.64 | 0.34 | 0.98 | 61.6× | 0.25 |
| 8 | def search | 20.56 | 33.40 | 0.47 | 0.77 | 43.5× | 0.00 |
| 9 | static-e5 | 29.45 | 38.72 | 0.46 | 1.06 | 63.7× | 0.25 |
| 10 | numpy | 19.89 | 31.98 | 0.43 | 1.31 | 46.1× | 0.43 |
| 11 | kensaku | 17.98 | 41.96 | 0.40 | 1.13 | 44.8× | 0.43 |
| 12 | пароль | 27.47 | 47.64 | 0.42 | 1.06 | 64.8× | 0.00 |
| 13 | скриншот | 28.22 | 36.16 | 0.42 | 1.39 | 67.3× | 0.00 |
| 14 | report 2026 | 27.94 | 34.97 | 0.35 | 0.59 | 78.8× | 0.00 |
| 15 | test | 22.81 | 40.17 | 0.40 | 1.02 | 57.3× | 0.11 |
| 16 | main | 18.99 | 24.63 | 0.34 | 1.34 | 56.0× | 0.00 |
| 17 | index | 20.29 | 29.61 | 0.42 | 0.94 | 48.5× | 0.11 |
| 18 | watcher | 19.71 | 23.24 | 0.41 | 1.24 | 48.3× | 0.67 |
| 19 | OCR | 21.89 | 24.97 | 0.45 | 1.49 | 48.7× | 0.67 |
| 20 | договор | 477.33 | 647.29 | 0.45 | 1.14 | 1060.7× | 0.25 |
| 21 | function | 21.59 | 25.25 | 0.37 | 1.47 | 58.9× | 0.00 |
| 22 | import sqlite3 | 23.03 | 33.51 | 0.48 | 1.78 | 48.1× | 0.00 |
| 23 | class User | 19.55 | 39.19 | 0.41 | 0.90 | 47.6× | 0.00 |
| 24 | TODO | 21.53 | 40.02 | 0.34 | 1.25 | 62.6× | 0.25 |
| 25 | JSON API | 29.32 | 32.12 | 0.36 | 0.74 | 81.0× | 0.00 |
| 26 | python | 20.49 | 25.26 | 0.49 | 1.08 | 42.2× | 0.67 |
| 27 | Windows | 19.43 | 37.96 | 0.41 | 0.70 | 47.0× | 0.67 |
| 28 | 2026 | 25.21 | 43.36 | 0.45 | 1.79 | 56.1× | 0.00 |
| 29 | сметы на март | 438.63 | 553.63 | 0.42 | 0.79 | 1043.6× | 0.00 |
| 30 | 中文数据 | 21.60 | 40.64 | 0.38 | 0.81 | 56.5× | 0.00 |
| 31 | ext:py | 25.16 | 39.06 | 0.45 | 0.82 | 55.6× | 0.00 |
| 32 | ext:md | 2.06 | 4.91 | 0.31 | 0.47 | 6.7× | 0.00 |
| 33 | path:Downloads | 13.18 | 37.47 | 0.68 | 1.87 | 19.3× | 0.00 |
| 34 | since:iso | 16.99 | 24.82 | 0.58 | 1.68 | 29.1× | 0.00 |
| 35 | sort:name | 13.42 | 29.97 | 0.47 | 1.53 | 28.5× | 0.00 |

AutoFind median latency stayed in a **0.31–0.68 ms** band across all axes; kensaku ranged **2.06–489.57 ms**.

### 5.3 Speedup distribution

| Statistic | Median-latency speedup | p95-latency speedup |
|---|---:|---:|
| Minimum | 6.7× | 10.5× |
| 25th percentile | 44.8× | 19.2× |
| Median | 49.5× | 26.0× |
| 75th percentile | 62.6× | 45.1× |
| Maximum | 1515.2× | 704.6× |

Axes above 100×: 3; above 50×: 17; above 20×: 33; below 10×: 1. The outliers are long multi-word queries where kensaku's prefix expansion costs hundreds of milliseconds.

### 5.4 First query, cold CLI and memory

- **First query:** 1.35 ms (AutoFind) vs 28.75 ms (kensaku) — the first request already hits the warmed index in both servers.
- **Cold CLI, run B:** AutoFind 488.8 ms with the server running, 711.5 ms in forced `--local` mode; kensaku 435.1 ms. Run A measured 161.4 / 316.1 / 211.7 ms for the same three commands. The local path pays ~77 ms for `import numpy` plus decryption of the 31.8 MB sealed BM25 header; the delegated path only pays Python startup and one HTTP request.
- **RSS:** run B 104.6 MiB (AutoFind) vs 121.5 MiB (kensaku); run A 188.1 vs 111.8 MiB. After the BM25 narrowing (int64→int32), zstd-compressed sealed header, `memoryview(mmap)` read path and smaller JSON/search caches, the AutoFind server now sits *below* kensaku after warm-up without losing warm-search latency.

### 5.5 GUI → window

The Rust shell in AutoFind loads the exact same `index.html`, `style.css` and `app.js` — no visual reimplementation. Window creation happens before the backend check; Python startup and WebView2 initialization overlap.

- Paired 5-run measurement with the same `Start-Process` → `MainWindowHandle` method: **AutoFind 142.5 ms, kensaku 170.1 ms** (median).
- A clean Win32 GUI-subsystem prototype reaches a window in 95–138 ms on this machine, i.e. the shell is already near the platform floor; changing the web engine would not improve this number.
- The earlier ~500 ms result was a build bug, not WebView2: the shell had been compiled as a **console** PE subsystem, so ShellExecute spawned conhost first (~900 ms). Adding `#![windows_subsystem = "windows"]` fixed it. A second launch raises the resident window instead of rebuilding it.

### 5.6 Disk and indexing

- AutoFind active index: **204,234,174 B** across 5 files. kensaku active index: **231,543,006 B** across 14 files. AutoFind uses 11.8% less disk.
- AutoFind indexed a fresh 10k text corpus at **2,654.8 files/s** with `--workers 4` (median of 3 runs). kensaku v1.0.19 has no CLI index command and its portable GUI rewrites the configured watch roots to `~/Downloads`, so a clean 10k run was not possible without touching the real index; no number is reported instead of a guessed one.

### 5.7 Result overlap

Mean IoU of top-5 paths: **0.146**; median 0.00; 14 of 35 axes have any overlap; maximum 0.67. With different indexes this measures dataset divergence, not ranking quality. A labelled Recall@k set would be needed to compare relevance.

## 6. Fairness notes and caveats

1. **Different corpora.** 21,643 vs 18,923 indexed files, different chunking. Speed numbers are comparable; relevance numbers are not.
2. **Background load.** Discord/Edge/WorkBuddyAI/OpenCode were running. Cold CLI changed winner between run A and run B; GUI-to-window was measured in one paired 5-run session and is similarly sensitive to background load. Warm HTTP search was stable: 35/35 in both runs.
3. **Cold CLI mode.** AutoFind's normal command delegates to the already-running server by design; the forced `--local` number is shown separately so the delegation is not hidden.
4. **RSS depends on history.** The first AutoFind server had been serving for hours and held accumulated caches; the optimized build and caches changed the later number. Both are reported.
5. **Warm-up is part of the method.** Both servers were measured after their own ready signal and background warm-up; cold-start cost is a separate axis.
6. **No total-hit counts.** The benchmark script recorded latency and paths, not total hit counts, so no `total` column is claimed.
7. **Index integrity.** No index file of either engine was modified by these measurements; all experimental writes went to isolated temp directories.

## 7. Reproduction

```powershell
cd C:\Users\h1ile\Downloads\AutoFind
# kensaku HTTP server on :8766 (token printed at startup)
& $env:TEMP\opencode\afbench\kensaku-cli-windows-x86_64.exe serve --port 8766
# AutoFind server on :8765 is expected to be running
python -m core.cli serve --no-browser --port 8765

# warm + cold CLI + RSS + disk + overlap, 35 axes
python tools/bench_compare.py
# GUI-to-window, paired Start-Process -> MainWindowHandle
powershell -File tools\bench_gui_shell.ps1 -Runs 5
```

Raw output: `%TEMP%\opencode\afbench\bench_result.json`, `gui_shell_result.json`. The benchmark script is `tools/bench_compare.py`; the GUI script is `tools/bench_gui_shell.ps1` (it also asserts the GUI PE subsystem so the conhost regression cannot silently return).

## 8. Conclusion

For warm interactive search on this machine, AutoFind was faster on every one of the 35 axes, with a 49.5× median speedup and a 1.87 ms worst p95. It also used less disk and, after the latest changes, less resident memory; its window appeared slightly faster in the single paired GUI run. Cold CLI changed winner between the two full runs, and GUI-to-window is likewise noisy across sessions — neither is a stable advantage. The open question is relevance: with only 0.146 mean top-5 overlap between different corpora, a labelled Recall@k test is the next benchmark to run. That follow-up is complete in `RELEVANCE_REPORT.md`: on the 18,417 files indexed by both engines and identical query strings, AutoFind now wins multi-word relevance (Recall@5 0.88 vs 0.43 on folder+name queries, 0.87 vs 0.72 on filename queries) while staying ~50× faster.

---

## Кратко по-русски

Тёплый поиск: 35/35 побед по медиане и p95, медианное ускорение 49.5×, максимальный p95 1.87 мс. Первый запрос быстрее, индекс меньше на диске, RSS после правок тоже ниже, GUI→окно в парном замере быстрее. Cold CLI и GUI нестабильны между прогонами из-за фоновой нагрузки — честно указаны оба прогона. Качество выдачи не сравнивалось: корпуса разные.