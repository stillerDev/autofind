# Benchmarks: AutoFind v12 vs Kensaku-new 1.0.17

Same machine, same generated corpora (synthetic text: word + index + extension
over ~100 folders, ~1 in 8 Cyrillic names, 6 extractable extensions, 2 large
files per 1000 so content search has substance). Kensaku built from source,
commit `9585797`, sandboxed with `KENSAKU_DATA_DIR`. AutoFind on schema v12.

Cold = CLI process start to result, 7 queries × 3 runs, best of each, median
over queries. Warm = HTTP round trip, 7 queries × 11-15 runs, median of each,
measured after every lazy build settles. Memory = server process private (USS).

## Indexing

| Corpus | AutoFind | Kensaku | AutoFind db | Kensaku db |
|---|---|---|---|---|
| 1 000 files | **1.9 s** (522/s) | 2.1 s (469/s) | 0.9 MB | 2 MB |
| 10 000 files | 7.2 s (1 391/s) | **2.5 s** (4 022/s) | 6.9 MB | 6 MB |
| 100 000 files | 42.7 s (2 343/s) | **24.3 s** (4 123/s) | 68.1 MB | ~113 MB |
| 1 000 000 files | 846 s (1 182/s) | **399 s** (2 505/s) | 660.8 MB | 291 MB |

Kensaku's Rust indexing scales flatter — roughly 2-3× faster from 10k up.
AutoFind's 1M run exposed and fixed a real O(n²): the `files` view trigger
swept orphan directories with a full-table `NOT IN` on every updated row;
the targeted cleanup restored a flat 1 182 files/s.

## Search

| Corpus | Cold AutoFind | Cold Kensaku | Warm AutoFind | Warm Kensaku |
|---|---|---|---|---|
| 1k | 227 ms | **78 ms** | **15.3 ms** | 24.7 ms |
| 10k | 269 ms | **116 ms** | 16.4 ms | **11.9 ms** |
| 100k | **382 ms** | 835 ms | **18.0 ms** | 29.9 ms |
| 1M | **1 601 ms** | 5 144 ms | **34.4 ms** | 107.9 ms |

- Tiny corpora: kensaku's Rust binary starts in ~80 ms against our ~200 ms
  Python boot — they own the 1k cold start, and 10k warm is a wash.
- From 100k the picture inverts and widens: 2.2× faster cold at 100k, 3.2× at
  1M; warm 1.7× at 100k and 3.1× at 1M. The 1M warm number is the fingerprinted
  mmap BM25 sidecar doing its job — postings leave private memory, and the
  content channel stays fast at any corpus size.

## Server memory (private, USS)

| Corpus | AutoFind serve | Kensaku serve (+daemon) |
|---|---|---|
| 1k | 38 MiB | 6 MiB (+daemon) |
| 10k | 62 MiB | 9 MiB (+daemon) |
| 100k | **57 MiB** | 101 MiB (+daemon) |
| 1M | 565 MiB | **341 MiB** (+daemon) |

kensaku's serve is lean at small corpora (nothing loaded yet); from 100k our
mmap-first design (nameindex blobs, BM25 postings, vector pack — all
file-backed) wins clearly. At 1M their 341 MiB beats our 565 MiB: the
remaining fat on our side is per-query Python state at that scale (a
1M-entry usage map, the packed-bits vector cache), which is the next
optimisation target.

## Where each engine wins

| | AutoFind | Kensaku |
|---|---|---|
| Process start | — | ✅ (~80 ms vs ~200 ms) |
| Indexing speed | 1k only | ✅ 10k-1M (2-3×) |
| Cold search | ✅ 100k+ (2.2-3.2×) | ✅ 1k-10k |
| Warm search | ✅ 1k, 100k (1.7×), 1M (3.1×) | ✅ 10k |
| Index size | ✅ 100k (−40%) | ✅ 1M (−56%) |
| Server memory | ✅ 100k (×1.8) | ✅ 1k, 10k, 1M |
| Content coverage | ✅ 104k chunks vs 8k docs | — |
| Text-to-image (ru/zh) | ✅ CLIP | — |
| Encryption at rest | ✅ AES-256-GCM | — |
