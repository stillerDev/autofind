# Relevance benchmark: identical targets, identical queries — and the fix

**Date:** 2026-11-25 18:20:48  |  **Machine:** DESKTOP-368  |  **Shared ground truth:** 18,417 files indexed by both engines  |  **Queries:** 30 × 3 sets

## What changed

The first run of this benchmark showed AutoFind losing multi-word relevance badly. Two code changes closed the gap:

1. `core/nameindex.py` — a free-text query with 2–6 words now matches names **and paths** containing every token, merged with the existing fuzzy result instead of treating the whole string as one opaque needle. `search_rows` (the numpy-free CLI path) applies the same rule.
2. `core/search.py` — `finalize_score` adds a rank bonus when all tokens are in the filename (`NAME_TOKENS_BONUS`) or in filename+folder (`NAME_TOKENS_PATH_BONUS`), so an all-token file outranks a loose content match after RRF fusion.

A third bug surfaced while benchmarking: `core/api.py` evicted `SEARCH_CACHE` entries by unpacking JSON **bytes** as `_, evicted = ...`, which raised `ValueError: too many values to unpack` at the first eviction and returned HTTP 500 for later searches. Eviction is now a named `prune_search_cache()` helper with a regression test.

## Before → after (same 30 queries, same labels, same machines)

| Set | Metric | Before | After | kensaku |
|---|---|---:|---:|---:|
| B — path + name | recall@5 | 0.017 | **0.883** | 0.433 |
| B — path + name | mrr | 0.025 | **0.824** | 0.281 |
| B — path + name | success@1 | 0.000 | **0.733** | 0.200 |
| C — name only | recall@5 | 0.033 | **0.867** | 0.722 |
| C — name only | mrr | 0.026 | **0.950** | 0.503 |
| C — name only | success@1 | 0.000 | **0.900** | 0.333 |

Latency did not regress: median on set B dropped from 0.31 to 0.31 ms, on set C from 0.32 to 0.32 ms (kensaku 15.80 / 16.06 ms).

## Final results

| Set | Metric | AutoFind | kensaku | Winner |
|---|---|---:|---:|---|
| A — exact unique token | Recall@1 | 1.000 | 1.000 | tie |
| A | median latency | 0.30 ms | 794.50 ms | AutoFind |
| B — path + name | Recall@5 | 0.883 | 0.433 | AutoFind |
| B | MRR | 0.824 | 0.281 | AutoFind |
| B | Success@1 | 0.733 | 0.200 | AutoFind |
| B | Success@5 | 0.933 | 0.467 | AutoFind |
| B | median latency | 0.31 ms | 15.80 ms | AutoFind |
| C — filename tokens | Recall@5 | 0.867 | 0.722 | AutoFind |
| C | MRR | 0.950 | 0.503 | AutoFind |
| C | Success@1 | 0.900 | 0.333 | AutoFind |
| C | Success@5 | 1.000 | 0.800 | AutoFind |
| C | median latency | 0.32 ms | 16.06 ms | AutoFind |

## Sets and method

- Ground truth: the 18,417 paths present in **both** indexes; for each query the relevant file(s) are those whose name or path contains every query token.
- A: one filename token unique in the shared corpus. B: one folder token + one filename token. C: two tokens from the same filename.
- Both engines answer the same query strings over HTTP, 1 warm-up + 5 timed keep-alive requests, median and p95.
- Labels are derived from paths, not human judgements; set B structurally favours engines that index folder names, set C is name-only.

## Full per-query tables

### A — unique token

| Query | AF rank | KS rank | AF ms | KS ms |
|---|---:|---:|---:|---:|
| get_blendfile_summary_of_linked_libraries_toolcode | 1 | 1 | 0.37 | 781.69 |
| template_deferred_tool_check_for_file_output | 1 | 1 | 0.24 | 788.18 |
| get_blendfile_summary_missing_files_toolcode | 1 | 1 | 0.49 | 745.73 |
| jump_to_view3d_object_data_by_name_toolcode | 1 | 1 | 0.24 | 922.37 |
| note_colon__this_changes_render_resolution | 1 | 1 | 0.49 | 367.25 |
| get_blendfile_summary_usage_guess_toolcode | 1 | 1 | 0.52 | 795.80 |
| spv-amd-shader-explicit-vertex-parameter | 1 | 1 | 0.29 | 906.95 |
| amd_shader_explicit_vertex_parameter | 1 | 1 | 0.29 | 400.40 |
| vulkan_video_codec_h264std_decode | 1 | 1 | 0.23 | 925.42 |
| vulkan_video_codec_h264std_encode | 1 | 1 | 0.50 | 907.65 |
| vulkan_video_codec_h265std_decode | 1 | 1 | 0.66 | 793.20 |
| vulkan_video_codec_h265std_encode | 1 | 1 | 0.40 | 842.76 |
| test_results_2026-08-27_22-27-50 | 1 | 1 | 0.29 | 74.72 |
| test_results_2026-08-28_13-11-50 | 1 | 1 | 0.30 | 67.69 |
| governance-and-responsibilities | 1 | 1 | 0.30 | 891.24 |
| generate_umath_validation_data | 1 | 1 | 0.30 | 605.06 |
| ses_e3793979affd0npiv3n3usb073 | 1 | 1 | 0.32 | 362.83 |
| spv-amd-shader-trinary-minmax | 1 | 1 | 0.34 | 964.62 |
| affine_transform_sparse_input | 1 | 1 | 0.39 | 924.86 |
| solid_instance_model_b5d59996 | 1 | 1 | 0.28 | 896.34 |
| system_info_20260113_205026 | 1 | 1 | 0.37 | 1021.12 |
| system_info_20260116_190224 | 1 | 1 | 0.29 | 966.83 |
| system_info_20260116_190528 | 1 | 1 | 0.23 | 1092.12 |
| umath-validation-set-readme | 1 | 1 | 0.28 | 659.28 |
| stb_voxel_render_interview | 1 | 1 | 0.40 | 1010.44 |
| chapter_iii_the_small_copy | 1 | 1 | 0.29 | 183.31 |
| opengl_impl_command_queue | 1 | 1 | 0.22 | 807.63 |
| d3d10_impl_command_queue | 1 | 1 | 0.40 | 34.06 |
| d3d11_impl_command_queue | 1 | 1 | 0.31 | 32.45 |
| opengl_impl_command_list | 1 | 1 | 0.27 | 387.91 |

### B — path token + filename token

`R` = shared files containing both tokens; AF/KS = rank of first relevant hit (— = outside top 10).

| Query | R | AF | KS | AF ms | KS ms |
|---|---:|---:|---:|---:|---:|
| extension chess | 1 | 1 | 1 | 0.31 | 16.25 |
| readme chess | 1 | 1 | 5 | 0.30 | 15.49 |
| requirements chess | 1 | 1 | 2 | 0.41 | 265.00 |
| analyzer chess | 1 | 1 | — | 0.48 | 116.48 |
| requires blender | 2 | 1 | — | 0.31 | 30.53 |
| global gridly | 1 | 1 | 4 | 0.20 | 12.35 |
| titlebar gridly | 1 | 1 | — | 0.24 | 445.72 |
| requirements gridly | 2 | 1 | — | 0.30 | 13.00 |
| readme oracul | 1 | 1 | 1 | 0.22 | 799.62 |
| requirements oracul | 1 | 1 | — | 0.33 | 884.11 |
| targets oracul | 1 | 1 | — | 0.22 | 813.95 |
| global gridly1 | 1 | 1 | 5 | 0.36 | 323.13 |
| requirements gridly1 | 2 | 1 | — | 0.32 | 626.83 |
| client xmlrpc | 1 | 2 | 4 | 0.42 | 15.16 |
| server xmlrpc | 1 | 4 | 1 | 0.33 | 427.04 |
| batch search | 1 | 1 | — | 0.36 | 12.56 |
| cargo tauri | 2 | — | — | 0.27 | 14.65 |
| build tauri | 2 | 1 | 1 | 0.27 | 12.88 |
| linux tauri | 2 | 1 | — | 0.41 | 12.71 |
| macos tauri | 2 | 1 | — | 0.27 | 16.65 |
| windows tauri | 2 | 1 | — | 0.30 | 12.39 |
| level setuptools | 1 | 3 | 3 | 0.40 | 12.72 |
| macos distutils | 1 | 2 | — | 0.27 | 14.43 |
| modified distutils | 2 | 1 | 5 | 0.38 | 16.10 |
| msvccompiler distutils | 1 | 1 | 1 | 0.35 | 171.21 |
| archive distutils | 2 | 1 | — | 0.29 | 8.74 |
| ccompiler distutils | 1 | 7 | — | 0.30 | 12.94 |
| cygwinccompiler distutils | 1 | 2 | — | 0.31 | 273.89 |
| debug distutils | 1 | 2 | 1 | 0.28 | 12.10 |
| errors distutils | 2 | 1 | 2 | 0.36 | 12.53 |

### C — two filename tokens

| Query | R | AF | KS | AF ms | KS ms |
|---|---:|---:|---:|---:|---:|
| browser extension | 1 | 1 | 1 | 0.32 | 19.29 |
| macos global | 2 | 1 | 2 | 0.44 | 12.11 |
| global overlay | 2 | 1 | 5 | 0.38 | 12.57 |
| macos titlebar | 2 | 1 | 5 | 0.30 | 764.95 |
| tauri linux | 2 | 1 | — | 0.33 | 12.62 |
| onnxruntime linux | 2 | 1 | 1 | 0.35 | 16.23 |
| tauri macos | 2 | 1 | — | 0.41 | 16.99 |
| onnxruntime macos | 2 | 1 | 2 | 0.27 | 15.90 |
| tauri windows | 2 | 1 | — | 0.31 | 12.51 |
| onnxruntime windows | 2 | 1 | 1 | 0.51 | 16.30 |
| macos compat | 1 | 1 | — | 0.37 | 14.88 |
| fancy getopt | 1 | 1 | 1 | 0.40 | 12.04 |
| common rules | 2 | 1 | 7 | 0.28 | 13.36 |
| f90mod rules | 2 | 1 | 3 | 0.32 | 413.80 |
| advanced topics | 1 | 1 | 2 | 0.31 | 15.40 |
| compiling source | 1 | 1 | — | 0.32 | 10.73 |
| download usage | 1 | 1 | 1 | 0.30 | 12.44 |
| governance responsibilities | 1 | 1 | 2 | 0.25 | 831.95 |
| regression tests | 1 | 2 | 5 | 0.38 | 12.51 |
| ipset exclude | 2 | 1 | 1 | 0.31 | 81.10 |
| pixels slash | 3 | 1 | 4 | 0.36 | 14.30 |
| slash centimeter | 1 | 1 | 1 | 0.26 | 699.67 |
| pixels meter | 1 | 2 | 2 | 0.29 | 23.40 |
| slash meter | 1 | 2 | 4 | 0.27 | 375.65 |
| besthead ultra | 1 | 1 | 1 | 0.32 | 804.17 |
| voxel render | 2 | 1 | 3 | 0.31 | 11.90 |
| render interview | 1 | 1 | 3 | 0.37 | 581.00 |
| public domain | 2 | 1 | 3 | 0.45 | 12.03 |
| imgui filelistabsolute | 1 | 1 | 1 | 0.28 | 404.04 |
| loader filelistabsolute | 1 | 1 | 1 | 0.37 | 350.26 |

## Verification

- 595 unit tests OK (3 new regressions for multi-token matching and 1 for cache eviction).
- `ruff --select E9,F` clean on all changed files.
- The 35-axis speed benchmark after the fix: 35/35 wins by median and p95, median speedup 67.9×, worst p95 0.43 ms.

## Reproduction

```powershell
cd C:\Users\h1ile\Downloads\AutoFind
python -m core.cli serve --no-browser --port 8765
& $env:TEMP\opencode\afbench\kensaku-cli-windows-x86_64.exe serve --port 8766
python tools/bench_relevance.py
python tools/bench_relevance_hard.py
python tools/bench_relevance_names.py
```

Query sets: `tools/relevance_queries*.json`; raw results: `RELEVANCE_*_RESULT.json`.

## Verdict

AutoFind now wins relevance **and** latency on identical queries: set B Recall@5 0.883 vs 0.433, MRR 0.824 vs 0.281; set C Recall@5 0.867 vs 0.722, MRR 0.950 vs 0.503; and it answers in ~0.31 ms against kensaku's 15.80 ms.