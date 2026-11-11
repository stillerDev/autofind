**English** | [Русский](README.ru.md)

# AutoFind / AutoFind

Local search over your files and screenshots. One SQLite file, no account, no
network calls at runtime.

The window is branded **AutoFind** — the folder the project lives in. The Python
package, the CLI and the index directory are still `autofind`; renaming those
would break every path in the docs for no functional gain.

The header follows the sketched layout: the forest photo (`web/fon.jpg`) as the
background, the wordmark top-left, the search field with a clear button
top-right, and the type filter as a row of pills directly under the title. The
photo is not confined to the header — it is the page background (fixed, `cover`),
with a single vertical veil over the whole window and translucent glass panels
on top, so results sit *on* the image and it shows through the gaps instead of
being cut off by a black list. The accent is white-grey: an orange button read as
a warning against fog.



metric autofind vs kensaku
<img width="1046" height="692" alt="image" src="https://github.com/user-attachments/assets/1de83f46-b6d7-4c39-9b0a-ca0a7c5894a7" />



## Status

All four layers of the plan are implemented and run on a stock Windows machine
with no model downloads:

| Layer | State | Engine |
|---|---|---|
| Text (PDF, DOCX, PPTX, XLSX, EPUB, ODT, RTF, TXT/MD, code) | works | built-in extractors, no deps |
| Screenshots: OCR | works | 3 tiers, 66 languages here: Windows packs → PP-OCRv6 multi → PP-OCRv5 per script |
| Screenshots: visual similarity | works | 228-dim layout/colour descriptor + perceptual-hash gate |
| Search | works | fuzzy names + BM25 + vectors, fused with RRF |
| Name search | works | in-memory name index, 1.7 ms at 100k names |
| Vectors | works | memory-mapped pack, ~40 ms warm, no 333 MB matrix |
| Result grouping | works | versions, identical copies and screenshot bursts, expandable |
| Query language | works | `type:`, `ext:`, `in:`, `modified:`, `size:`, `re:`, … |
| Natural language | works | deterministic RU/EN intent compiler with removable chips |
| Agent access | works | MCP over stdio **and** HTTP (`autofind connect`) |
| CJK screenshots | works | Japanese, Chinese and Korean read via RapidOCR, not just retried |
| Live updates | works | polling watcher, ~2 s debounce |
| Quality measurement | works | `eval` with Recall@10, MRR, ablation |
| Privacy filter | works | path blocklist + `.gitignore` parity, `privacy purge` |
| App | works | native window (WebView2), tray, `Ctrl+Alt+Space`, autostart |
| Interface | works | English by default, Russian in one click, index in a drawer |
| Appearance | works | five walls or your own picture; the palette is derived from it |
| Personal layer | works | pins, saved searches, dictionary — and `import kensaku` |
| Code definitions | works | `sym:` over nine language families, no tree-sitter |

Two upgrades are drop-in and change nothing else: `multilingual-e5-small` for
semantic text vectors, and `CLIP ViT-B/32` for text-to-image search. Until they
are present the text embedder is lexical (words + character 4-grams) and image
search is similarity-only, and the UI says so instead of pretending.

## Quick start

```bat
AutoFind.bat
```

or

```bash
python run.py                 # opens the app window
python run.py --browser       # the same UI in a browser tab, if you prefer
```

`run.py` opens a real window: its own icon, its own title bar, and it stays in
the tray when you close it. `Ctrl+Alt+Space` brings it back from anywhere, and
launching it a second time raises the window that is already running instead of
starting a rival copy.

To make it permanent — start with Windows, in the tray, no console window:

```bash
autofind install               # autostart entry + desktop and Start Menu shortcuts
autofind autostart status      # what the login entry points at
autofind autostart off         # undo it
```

The login entry is one value under `HKCU\...\Run`, so it also appears in Task
Manager → Startup where it can be switched off like any other program. It runs
`pythonw.exe`, not `python.exe`: the console window is the difference between an
app and a script.

The sidebar lists the folders worth indexing (Documents, Downloads, Desktop,
Screenshots) — one click indexes them, progress is live, and results appear as
files are added.

Command line:

```bash
python -m core.cli index ~/Documents ~/Downloads ~/Pictures/Screenshots --prune
python -m core.cli watch ~/Documents ~/Downloads          # keep the index in sync
python -m core.cli search "неверный пароль при входе"
python -m core.cli search "cannot find module" --kind image
python -m core.cli search "скриншоты за прошлую неделю"   # natural language
python -m core.cli search 're:"TODO|FIXME" ext:py'        # query grammar
python -m core.cli search "смета" --sort newest -n 20 --offset 20
python -m core.cli mcp                                     # MCP server for AI agents
python -m core.cli cheat                                   # print the grammar
python -m core.cli eval tests/queries/labels_demo.json --ablate --verbose
python -m core.cli doctor                                  # environment + bundle budget
python -m core.cli privacy                                 # what the filter would skip
python -m core.cli privacy purge                           # drop it from the index
python -m core.cli stats
```

## Query language and natural language

Two ways to say the same thing. Explicit grammar is authoritative — if the query
already contains a `key:value`, the intent compiler leaves it alone.

| Syntax | Meaning |
|---|---|
| `type:pdf` | category (`image`, `doc`, `sheet`, `presentation`, `code`, `video`, `archive`) or an extension |
| `ext:rs` | exact extension |
| `in:"C:\Docs"` | only inside this folder (Windows paths keep their backslashes) |
| `path:src` | substring of the path |
| `modified:<7d` | changed within a window: `1h`, `7d`, `30d`, `7d..30d` |
| `size:>2mb` | `>`, `<` or `1mb..10mb` |
| `lang:python` | language family of the extension |
| `name:config` / `content:timeout` | match the filename / the contents only |
| `sym:parse_query` | a definition in code — function, class, struct, type, method |
| `re:"TODO\|FIXME"` | regular expression over content |
| `-node_modules` | exclude a term or folder |
| `"exact phrase"` | phrase |
| `mode:exhaustive` | every match, path-sorted, no ranking |
| `sort:newest` | `newest`, `oldest`, `largest`, `name` |

Plain phrasing is compiled into the same grammar, locally, with no model:

| You type | It becomes |
|---|---|
| `скриншоты за прошлую неделю` | `type:image modified:<7d` |
| `самые большие файлы` | sort by size |
| `3 дня назад ошибка` | `ошибка modified:4d..3d` |
| `вчерашние документы` | `type:doc modified:1d..2d` |
| `pdf про индексы` | `индексы type:pdf` |
| `screenshots from last week` | `type:image modified:<7d` |

Every inference comes back as a chip above the results with the words that
produced it; clicking the chip removes those words and re-runs. Nothing is
rewritten silently, and an unrecognised phrase is searched literally.

## Agents (MCP)

```bash
python -m core.cli mcp        # JSON-RPC 2.0 over stdio, read-only
python -m core.cli connect    # write the client config for you
```

Tools: `search` (with cursor pagination), `read_snippet` (bounded),
`index_state`, `regex_search`, `find_symbol`, `similar_images`. Read-only by
construction — there is no tool that can open, move or modify anything.
`find_symbol` is the one an agent uses most: it answers with the file, the line
and the definition itself, so a "where is this function" question costs one call
instead of reading files until one looks right.

Two transports, because the right one depends on the client:

| Transport | Endpoint | When |
|---|---|---|
| stdio | `autofind mcp` | Claude Desktop, Cursor — the client owns the process |
| HTTP | `POST http://127.0.0.1:8765/mcp` | sandboxed agents, remote tools, no process to spawn |

`autofind connect` writes the entry into `claude_desktop_config.json` or
`~/.cursor/mcp.json`, merging with the servers already there and keeping a
`.bak` of the file it changed. `--transport http` writes the URL form instead,
with a bearer token generated once into `~/.autofind/mcp_token`. `--write` is
required to actually touch a file; without it the command prints the diff, which
is what you want the first time.

```bash
autofind connect                      # show what would change, all clients found
autofind connect cursor --write       # edit one client
autofind connect --transport http --write
autofind connect print                # paste-ready JSON
```

## How it works

```
files ──► extractors ──┐
                       ├──► chunker ──► text embedder ──┐
screenshots ──► OCR ───┘                                ├──► index.db
                                                        │    FTS5 + vectors
screenshots ──► visual descriptor ──────────────────────┘
                                                             │
                              fuzzy names ─┐                 │
                                            ├─► RRF ─────────┘
                              BM25 ─────────┤
                              KNN  ─────────┘
```

Screenshots land in the same `chunks` table as documents, so one query returns
a PDF and a screenshot side by side. Each screenshot also gets a thumbnail and
a visual vector, which powers "similar screenshots" in the UI.

**"Similar" is filtered, not padded.** The layout cosine only nominates
candidates; a candidate is returned when it is within 8 bits of the source's
63-bit perceptual hash or scores at least 0.99 on the cosine. The descriptor is
non-negative, so unrelated screenshots still score ~0.9 and the nearest twelve
always existed whether or not any of them resembled the source. Now a click
returns the genuine matches and says "no similar images" when there are none,
rather than filling the modal with whatever ranked highest.

**Three channels, one ranking.** Fuzzy name matching (a 64-bit character bloom
rejects most of the corpus before the matcher runs, so `rdme` finds `README.md`),
BM25 over chunk text, and cosine over the chunk vectors. The fused score is
normalised to `[0, 1]` and then adjusted: exact name +0.20, name prefix +0.15,
smooth recency `0.10·e^(−age/7d)`, frecency `0.08·log1p(opens)·e^(−last/14d)` from
files you actually opened, and a depth penalty bounded at −0.10 so a file twelve
folders deep cannot win on path shape alone. Results are **per file**, not per
chunk: a document matching three times is one row with its best snippet.

**Storage.** One SQLite file in WAL mode: `files`, `chunks`, `chunks_fts`
(FTS5), `images`, `vectors`, `usage`, `meta`. Vectors are float32 blobs searched
by brute force in numpy — at 50k chunks that is a 77 MB matrix and single-digit
milliseconds, so an ANN index would be premature. `sqlite-vec` can replace
`Store.knn()` without touching callers.

**Indexing is incremental.** Unchanged files are skipped by size+mtime, moved
files by sha256. Screenshots are deduplicated by sha256 before OCR runs.
`.gitignore`, `.cursorignore` and `.autofindignore` are honoured during the walk,
so build output a repository already ignores never reaches the index.

**Live updates** (`watch`) poll the roots every 2 s instead of using inotify or
ReadDirectoryChanges: one stat per file is cheap, and polling survives network
shares and cloud-synced folders where native notifications are unreliable. A
file is read only after it has been quiet for a second, so a document being
saved is indexed once at the end. On start the watcher also drops index entries
whose files no longer exist — otherwise a file deleted while the app was closed
would stay searchable forever.

## The app

`core/app.py`. pywebview hosts the same HTML in a WebView2 window — the system's
own Chromium, so nothing is bundled and the CSS renders exactly as it does in a
browser. The server runs in the same process on loopback: one process, one
index, one vector cache. A second process would mean a second port and a second
copy of the pack open at once.

- **No title bar.** The window is frameless and sized to the desktop *work area*,
  so it stops above the taskbar instead of covering it — the clock and the tray
  icon this app hides in are down there. The Windows title bar is four buttons
  the UI already replaces: a ✕ in the header corner, wired to the pywebview
  bridge (`js_api`) rather than to `window.close()`. `--windowed` brings the
  frame, the saved geometry and a resizable edge back.
- **Close means hide.** The window goes to the tray; the index and the server
  stay warm, which is the point of a launcher. Quitting is explicit, in the tray
  menu. If the tray and the hotkey both failed to start, closing really closes —
  hiding with no way back would strand the process.
- **`Ctrl+Alt+Space`** shows and hides it from anywhere. `--hotkey` changes it,
  `--no-hotkey` removes it.
- **A second launch raises the first.** The running app writes its port to
  `~/.autofind/app.port` and answers `POST /api/focus`; the new process asks and
  exits. An instance lock (`msvcrt.locking`, released by the OS on crash) is what
  stops two windows with two ports in the first place.
- **The window remembers.** Size, position and maximised state live in
  `~/.autofind/window.json`, clamped to the current screen so a window from a
  bigger monitor cannot open half under the taskbar. Borderless mode ignores the
  file: its size is the work area, not a preference.
- **Dark title bar and the app icon** via `DwmSetWindowAttribute` and
  `WM_SETICON`, for the `--windowed` case. The WinForms host defaults to a light
  title bar above a near-black UI, which looks like a screenshot pasted into a
  form.

`tools/make_icon.py` draws `web/icon.png` and `web/icon.ico` (16–256 px), so the
icon is reproducible instead of a binary nobody can regenerate.

## The interface

Two things about the layout are decisions rather than taste.

**The index is a drawer, not a column.** It used to be a permanent 320 px column
on the right, which cost a third of the result width for four panels that are
read once and then ignored for the rest of the session. It now opens over the
results from one button in the header, with a scrim that is also a close button,
and closing it never reflows the list you were reading. It floats — inset from
the window edge, rounded on all four corners — so it reads as a card laid over
the results rather than as a wall attached to the side of the window. Everything
the column held is still there — counts, roots, watch state, privacy, engines,
shortcuts.

**Results arrive one at a time.** A new result list fades and lifts each card in
38 ms steps, capped at the thirteenth, instead of flashing a fully formed page;
`prefers-reduced-motion` turns the cascade off. The duplicates report in the same
drawer is memoised per index stamp, so reopening it is instant, and its first
scan streams a percentage instead of leaving a spinner.

**Every word is a key.** `web/i18n.js` holds two dictionaries, English as the
source language, and the markup carries `data-i18n` attributes; anything drawn
by `app.js` goes through `t()`. A language switch is one call: it re-renders the
static text, the chips, the status line and the idle screen from the same data,
and remembers the choice in `localStorage`. English is the default, including
for a machine whose Windows is in Russian.

The search engine does not own any words. A chip arrives from the server as a
*kind* and a *value* — `{"kind": "week"}` or `{"kind": "daysAgo", "value": "3"}`
— and the interface decides how to say it, because "modified: in the past week"
is a sentence and the compiler is not the thing that knows the language of the
person reading it. The same applies to hints and to type values. `tests/test_i18n.py`
fails the build if a kind has no sentence in either language, if a key exists in
one dictionary and not the other, or if a `data-i18n` attribute names a key that
does not exist.

Russian needs three plural forms and English two; the rule lives next to the
strings. `3 snapshots` and `3 снимка` come from the same call.

The date filter is the same story in a smaller box. A native `<input type="date">`
draws its ghost text and opens its picker in the *OS* locale and cannot be told
otherwise from script: an English interface on a Russian Windows still said
"дд.мм.гггг" and opened a Russian calendar. So the calendar is ours — rounded,
grey, Monday-first, with today as a ring and the chosen day as a filled cell,
and its month and weekday names come out of the same dictionary as everything
else.

The gear opens the same dialog in both languages: the wall picker at the top,
the segmented switch, and an About list that reports what this install actually
is — files and chunks, the database size, how many languages the OCR can read
here, which embedders are loaded, and the version.

### The background is the theme

A dark interface next to a photograph is usually a black rectangle with a photo
behind it. Here the picture *is* the palette: every surface is
`hsl(var(--hue) var(--sat) …)`, the picker sets two numbers, and the panels,
borders, chips and hover states follow. Pick the brown wall and the borders go
warm; pick the ocean and they go teal.

Once a picture is yours, you rarely want the whole of it. The custom tile opens
two more controls, hidden for the gradients: **Position** (top / center / bottom)
anchors the edge the crop pins to, and **Zoom** (±) pushes in or pulls back.
Both are CSS only — `background-position` and a `transform: scale()` on the wall
layer — so the change is instant and survives reload. The hue is still read from
the whole image, not the cropped slice.

That means a picture you supply has to answer one question: what colour is it?
An RGB average cannot answer it — the mean of a sunset is grey, because the
orange and the blue cancel out. The measurement is a **circular mean of hues**,
weighted by saturation and ignoring the washed-out pixels:

```js
sum += saturation * sin(hue);   // hues are angles, so they add as vectors
cos += saturation * cos(hue);
hue = atan2(sum, cos);
```

Two smaller decisions come out of the same idea.

**The wallpaper is a file, not a `localStorage` entry.** A 4 MB photo in browser
storage is a quota error waiting for the wrong day, and it would die with a
cache clear. It is written to `~/.autofind/background.<ext>` next to the index —
so it survives, it travels with a backup of the config directory, and
`GET /api/background` serves it back with a cache-busting query. Only one is kept
at a time: a new PNG deletes the old JPG, because two files would leave the
reader picking whichever extension it happened to try first.

**The first frame is already the right colour.** `app.js` runs at the end of the
body, which is one paint too late: the window would flash the default forest
background and then jump to yours. A twelve-line script in `<head>` reads the
stored choice and sets `--wall`, `--hue`, `--sat`, `--wall-pos` and `--wall-scale`
on the root element before anything is rendered. `tests/test_theme.py` checks
that the script is in the head, that it reads the same storage key `app.js` writes,
and that every surface in the stylesheet is derived from the hue rather than
pinned to a hex value.

## What you bring with you

Nobody switches search app because of the search. They stay where they are
because of the twenty files they pinned, the four queries they run every week,
and the ten words they taught it. Those are the expensive things, and leaving
them behind is the reason a better search engine loses to a worse one.

So they are part of the product, not an afterthought:

**Pins.** One star on any result, and the file sits on the empty screen from
then on. A pin is a bonus in the ranking, not a filter: it lifts a file by 0.12
— more than recency (0.10), less than a name match (0.15) — so pinning a
screenshot does not hide the document you were actually looking for.

**Saved searches.** The bookmark inside the search box stores the query as
typed, including its filters. Saving the same query twice renames it instead of
duplicating it, and the list is ordered by how often you actually run it.

**The dictionary.** `cv → резюме` in Settings, and from then on typing `cv`
searches for `резюме` as well. Expansion is whole-word and case-insensitive and
never fires inside a `key:value` pair — `type:cv` still means `.cv` files — and
it is capped at eight replacements so a bad entry cannot grow a query without
bound. What fired is shown as a chip above the results, next to the ones the
intent compiler produced, and removing the chip re-runs the query without it.
The words live in Settings, above About, with an `×` per row:

**Definitions.** `sym:parse_query` answers with the definition rather than the
file: exact name first, then prefix, then substring, and the row says `function
parse_query · line 214` with the source line under it. Nine language families
are recognised by pattern — Python, JS/TS, Rust, Go, Java/Kotlin, Ruby, PHP,
shell, SQL — because a dependency-free extractor that is right about 95% of
definitions beats a tree-sitter build that costs 40 MB and a compile step on
Windows. Comments are skipped, so `# def not_real():` is not a hit.

All three live in the same SQLite file as the index, in `pins`, `saved` and
`aliases`. That means they travel with a backup, any SQLite client can read
them, and they never leave the machine — which is also what makes the migration
a copy rather than a retyping:

```bash
autofind import kensaku              # find the config, show what would move
autofind import kensaku --write      # copy pins, saved searches, dictionary
autofind import kensaku "C:\Users\you\AppData\Roaming\kensaku"
```

The reader does not assume a schema. kensaku is a Tauri app, so its store is one
of a handful of known places and one of two shapes, JSON or SQLite; the reader
walks whatever it finds and recognises pins, queries and aliases by name. A pin
whose file is not in the index is reported by path rather than silently dropped,
because "index that folder and run this again" is a better answer than a
favourite that quietly did not arrive.

The same things are on the command line, for scripts and for a machine where
the window is not open:

```bash
autofind pin "C:\Docs\contract.pdf"   # --off to unpin
autofind pins
autofind saved "type:pdf invoices" --label "Invoices"
autofind alias cv "резюме"
autofind sym parse_query -n 5
```

## Measured on this machine

Windows 11, Python 3.13, 4 cores, hashed-feature text embedder, Windows OCR:

| Corpus | Files | Chunks | Index time | Query (warm) |
|---|---|---|---|---|
| 6 screenshots + plan document + this repo | 36 | 174 | 0.7 s | 7–10 ms |
| Synthetic 1000-file mix (txt/py/docx) | 1 000 | 1 000 | 1.1 s | 8 ms |
| Real `~/Downloads` tree (Blender, SteamCMD, projects) | 8 373 | 216 998 | ~14 min | 21 ms |

OCR costs about **86 ms per screenshot** including preprocessing (6 screenshots
in 0.5 s). The 1 000-file run is 881 files/s against the plan's 10-minute
target, but that corpus is 1.5 MB total with one chunk per file — PDF-heavy
folders will be an order of magnitude slower, because extraction, not embedding,
is the bottleneck.

Two numbers the plan should have known:

- **Cold start used to cost 2.6 s** on the 217k-chunk index: the vector matrix
  (333 MB) was read from SQLite and stacked. It is now a memory-mapped pack —
  see the next section — so the first query after a restart costs the same as
  the second.
- **The index is 1.06 GB for 8.4k files**, plus a 320 MB pack. Most of it is
  float32 vectors, not text. Deduplicating identical chunks across the tree
  would cut it substantially.

### Against Kensaku

Measured side by side with Kensaku-new 1.0.17 on the same machine and the same
20 461-file index. Medians over the query set; CLI is 7 queries × 5 runs, HTTP
is 7 queries × 25 runs.

| Metric | AutoFind v9 | Kensaku-new 1.0.17 | Result |
|---|---|---|---|
| Cold CLI | 170.3 ms | 217.8 ms | 28% faster |
| Warm HTTP | 7.3 ms | 13.7 ms | 47% faster |
| Search (median) | 27.9 s | 31.1 s | 10% faster |
| Index on disk | 222.3 MB | 226.5 MB | ≈ same |
| Memory | 102.9 MB | 104.9 MB | overlap |
| Index coverage | 20 461 files, 311 235 chunks | 18 745 files, 8 447 full-text docs | wider coverage |

Chunks and full-text documents are different units, so the last row is about
reach rather than a like-for-like count.

## Scaling past 100k files

Three things used to grow with the corpus in a way that would have broken at
100k files. All three were replaced, and each number below is measured, not
estimated.

### Names live in memory, not in SQLite

`core/nameindex.py`. Every name, path and kind is loaded once into flat arrays
with a 63-bit character bloom, and the bloom prefilter is one vectorised AND over
the whole corpus. Literal-substring queries are answered by a single C-level
regex scan over a NUL-joined byte buffer, scored in numpy — so the cost tracks
the number of hits, not the size of the index.

| Names | Before (SQL scan) | Now, literal | Now, abbreviation |
|---|---|---|---|
| 8 000 | 35 ms | 0.2 ms | 1.7 ms |
| 100 000 | — | 1.7 ms | 19 ms (capped) |
| 1 000 000 | — | 20 ms | 45 ms (capped) |

The abbreviation tier is capped at 20k candidates, newest first, which is what
keeps it **bounded** instead of linear: a query over 1M names costs the same as
over 100k.

### Vectors are memory-mapped, not stacked

`core/vectors.py`. The 333 MB float32 matrix used to be read from SQLite and
copied into a numpy array at startup. It is now a packed side file
(`index.db.v4.vec`) mapped into the process: no copy, no anonymous 333 MB, and the
pages stay in the OS cache across restarts.

| Backend | Cold start | Query, 217k chunks | Extra disk |
|---|---|---|---|
| numpy matrix (before) | 2 600 ms | 21 ms | — |
| sqlite-vec `vec0` | 0 ms | 90–255 ms | +333 MB |
| memory-mapped pack | ~0 ms | 40 ms | +320 MB |

`sqlite-vec` is implemented and available (`--vector-backend sqlite-vec`) but is
not the default: its exact KNN walks shadow rows one at a time and costs 4–12x
more per query than one matmul. The pack is a pure cache — `vectors` in SQLite
stays the source of truth, so deleting the file costs a rebuild, never data.

The version in the name is load-bearing. Bumping the format while an older
process still had the old file mapped meant Windows refused the rename, the
build was thrown away, and every text query fell back to reading all vectors
out of SQLite: **2 860 ms against 12 ms**, measured twice in a row on the real
index before this was understood. Two versions now never share a file, and a
build that still cannot land is served from its temp name instead of being
dropped. `docs/decisions.md` §49.

### Near-duplicates collapse

`core/group.py`. Two rows that say the same thing are one row with a badge:

- **versions** — `resume.docx`, `resume_v2.docx`, `resume-final.docx`,
  `resume (1).docx`, `итоговый_отчет_в2.docx` group by a normalised stem inside
  one folder. Conservative on purpose: merging two genuinely different documents
  is worse than missing a merge, so `report` and `report_2024` stay apart.
- **copies** — the same file copied into two folders is one row. Same name and
  same sha256, different path: `openWallpaper\...\steambootstrapper_russian.txt`
  and `zz\...\steambootstrapper_russian.txt` are one answer, not two. Same
  bytes under a *different* name stay separate, because a name is usually the
  reason the user kept the second copy. On this corpus that is 611 rows (7%)
  that used to be noise.
- **bursts** — screenshots of the same screen within 15 minutes, in the same
  folder, at the **same pixel size**, within 10 bits of a 63-bit dHash **and**
  sharing 60% of their OCR words. Every one of those conditions comes from a
  false positive on real data: dHash resizes to 9×8 first, so it is blind to
  scale and scored one icon exported at 30, 44, 71 and 1024 px as identical.
  Hence the size floor (640 px — a screenshot is a screen, not a 32×32 sprite
  frame) and, when neither side has OCR text to confirm the match, a near-exact
  hash (≤2 bits). A wrong merge hides a file; a missed one leaves an extra row.

Clicking the badge expands the group. The badge is a fact, not decoration: it
tells you the row you see is the one the ranker picked out of N.

Bursts need a perceptual hash, which arrived in schema v4. An index built by an
older version gets it backfilled at the end of `autofind index` — decode only, no
OCR, 1 097 images in 3.8 s here. Without it the detector would silently see no
hashes on exactly the screenshots that were indexed first, since an unchanged
file is never re-indexed.

## Quality

`autofind eval` scores a labelled query set. On the 14-query smoke set over the
8.4k-file corpus:

| Channel | Recall@10 | MRR | p95 |
|---|---|---|---|
| BM25 only | 0.714 | 0.620 | 22 ms |
| Vector only (hashing fallback) | 0.071 | 0.073 | 39 ms |
| Fused (shipped default) | 0.714 | 0.620 | 59 ms |

Read this as a diagnosis, not a score. With the fallback embedder the vector
channel adds nothing and, at equal weight, actively hurt (0.643 recall before
damping). Two changes came out of the measurement: the vector weight now comes
from the embedder itself (0.3 for the hashed fallback, 1.0 for a semantic
model), and a query with no lexical match returns nothing instead of hash
collisions. The remaining misses are all paraphrases — «ошибка сборки» for a
screenshot that says `Error: cannot find module` — which is precisely what e5 is
for, and exactly what this build cannot do.

The 20-query acceptance set from the plan is in `tests/queries/queries.json`;
labels are still placeholders. Recall@10 ≥ 0.7 stays unproven until someone runs
it on a real corpus with answers written by a second person.

## OCR notes

The Windows recogniser ships with the OS and knows Russian if the language pack
is installed — no Tesseract binary, no 20 MB of traineddata, no install step.

Preprocessing is engine-specific, and this was measured rather than assumed:
feeding a dark screenshot through grayscale + autocontrast **lowers** the
Windows engine's output (136 vs 162 characters on the same image), because it
handles dark themes natively. So it gets the image as-is plus an upscale for
small text; only Tesseract gets the grayscale-and-invert treatment.

Engine order: `windows-ocr` → `tesseract-cli` → `pytesseract` → `rapidocr`.
The engine that produced a screenshot's text is recorded in the index, so a
later switch is visible instead of silent.

**66 languages.** Three tiers, and the order is the design:

| Tier | Engine | Languages | Cost |
|---|---|---|---|
| 1 | Windows OCR, *every* installed pack | `ru`, `en-US` here | 6–60 ms |
| 2 | rapidocr PP-OCRv6 `multi` | 53: all Latin-script, Japanese, Chinese | ~1.9 s |
| 3 | rapidocr PP-OCRv5 per script | Korean, Arabic, Cyrillic, Devanagari, Greek, Thai, Tamil, Telugu | ~1.2 s each |

Tier 1 tries every installed language pack rather than the display language: on
this machine `en-US` reads Russian as `npl'1BeT` and the `ru` pack reads it
correctly, for the same 10 ms. Tiers 2 and 3 only run when the result is
implausibly small for the amount of ink in the image — the same measured gate as
before — so a Russian screenshot costs 10 ms and a Japanese one costs 2 s
instead of being indexed as stray Latin letters.

Measured end to end on rendered test images:

| Image | Engine that won | Time | Text |
|---|---|---|---|
| English | `windows-ocr` | 0.01 s | exact |
| Russian | `windows-ocr` (`ru`) | 0.01 s | exact |
| Chinese | `rapidocr:multi` | 2.0 s | `你好世界 中文测试` |
| Japanese | `rapidocr:multi` | 3.4 s | `こんにちは世界 日本語のテスト` |
| Korean | `rapidocr:korean` | 5.3 s | `세계 … 테스트` |
| Arabic | `rapidocr:arabic` | 7.2 s | Arabic script, partially correct |

The tail is expensive on purpose, and bounded in two ways. Models that are not
on disk are never downloaded in the middle of an index run — `autofind ocr
download all` fetches them once (9 models, 70 MB). And the chain switches itself
off after eight consecutive attempts that changed nothing, so a folder of
photographs stops paying for the fallback after ~10 s while a folder of Japanese
screenshots — where every attempt succeeds — never hits the limit.

`autofind ocr languages` prints the languages readable *right now* with their
greetings; `autofind doctor` prints the count, the models on disk and the
installed packs. `ocr_cjk_model` still pins a custom recognition model.

## Cold start

`tools/coldstart.py` spawns the real app and polls until the index is warm,
timing three separate milestones. Three runs, median:

| Milestone | Median | What it means |
|---|---|---|
| port answers | 0.65 s | Python is up, the server thread is running |
| index ready | 0.77 s | vector pack faulted in — `/api/ready` flips |
| window visible | 0.96 s | a borderless window titled AutoFind is on screen |
| first search | 148 ms | one query against the now-warm index |

The gap between "port answers" and "index ready" is 120–160 ms of prewarm; the
rest is interpreter start plus `import webview`, which no amount of our own
optimisation removes. The gap between "index ready" and "window visible" is
WebView2 painting its first frame.

A true cold start also needs the OS page cache dropped, which needs
administrator rights; this measures a start with the index on disk and a
lukewarm file cache. The median of several runs is the number to quote.

## The boot screen

Because the window paints ~190 ms after the index is already warm, the splash is
not covering a slow load — it exists so the first thing on screen is not an
empty grid. It shows one greeting, changing every 700 ms, drawn from the
languages this machine's OCR can actually read: **the languages on screen are
exactly the ones the index can find text in.** On this machine that is 66 of
them, so the greeting is as likely to be `こんにちは` or `안녕하세요` as `Hello`.

It is bounded at both ends — minimum 900 ms so it never flashes, maximum 8 s of
polling so it can never hang — and Escape or a click dismisses it. A deep link
(`?q=` or `?preview=`) skips it: a link to a file is not a request for a
greeting.

## Embedders

| Role | Default | Upgrade |
|---|---|---|
| Text | `hashing-384` (words + char 4-grams) | `e5-small` int8 ONNX |
| Visual | `layout-228` (colour, layout, edges) | `CLIP ViT-B/32` int8 ONNX |

Drop weights in and restart:

```
~/.autofind/models/e5-small/model.onnx + tokenizer.json
~/.autofind/models/clip/visual.onnx + textual.onnx + tokenizer.json
```

`pip install 'autofind[onnx]'` adds the runtime. The index records which embedder
built it and refuses to mix two, so switching means reindexing — deliberate, not
an oversight.

## Bundle budget

`autofind doctor` adds up what would actually ship:

```
bundle       378.0 MB of 150 MB budget  over by 228.0 MB
  python-embed                  15.0 MB   runtime
  numpy                         18.0 MB   runtime
  pillow                         4.0 MB   runtime
  winrt bindings                 3.0 MB   runtime
  onnxruntime                   50.0 MB   optional: only with e5/CLIP
  app code + web                 1.0 MB   runtime
  model:e5-small               120.0 MB   download or bundle
  model:clip-vit-b32            45.0 MB   download or bundle
  model:tesseract-rus-eng       22.0 MB   download or bundle
  model:argos-ru-en            100.0 MB   download or bundle
```

The plan's ≤150 MB installer cannot hold e5 + CLIP + Argos. Dropping Argos
(Russian queries would then need a multilingual CLIP export, or text-to-image
search stays English-only) saves 100 MB and still leaves 278 MB. The realistic
split is a ~60 MB shell plus models downloaded on first run, or a hard choice
between semantic text and text-to-image.

## Privacy

Two separate promises, and the second one was missing:

1. **Nothing leaves the machine.** No network calls at runtime; the HTTP server
   binds to 127.0.0.1 and rejects foreign Host headers.
2. **Some things never enter the index.** `core/privacy.py` drops password
   stores, key material, browser profile data and cloud-credential folders
   *before* any file is opened: `.ssh`, `*.kdbx`, `wallet.dat`, Chrome/Edge
   profile directories, `*.env`, `*password*`-style names. Source files are
   exempt from the broad name globs — Blender ships `secrets.py`, and indexing a
   standard-library module is not an incident.

The filter is on by default, its count appears in `index` output and in the UI,
and `autofind privacy purge` removes what slipped in earlier. On this machine it
found two «Пароль на архив» files that predate the filter.

Still missing: **the index is not encrypted**. Filtered paths are skipped, but
everything else — including OCR text of your entire screen — sits in a plain
SQLite file. An app-level blocklist, a pause switch and SQLCipher are open items.

## Tests

```bash
python -m unittest discover -s tests -t .
```

CI (`.github/workflows/tests.yml`) runs the same suite on Windows for Python
3.11–3.13, plus a Linux smoke job that compiles the package and imports the core
modules.

312 tests: chunking, kind detection, DOCX/PDF/PPTX/XLSX/EPUB/ODT/RTF extraction,
image preprocessing, visual features, thumbnails, real OCR including Japanese
(skipped when no engine), the language table and the boot screen's greeting
pool, `/api/ready`, FTS query sanitising, the full index→search cycle, kind filters,
incremental skips, screenshot dedup, similar-image search, the privacy
blocklist, `.gitignore` parity, watcher diffing and startup cleanup, the eval
metrics, the bundle estimate, the query grammar and intent compiler, fuzzy name
scoring, the ranking bonuses, pagination, the MCP surface, the name index, the
vector pack, grouping, the desktop-app plumbing (lock, geometry, autostart),
symbol extraction per language and the `sym:` channel, pins as a bonus rather
than a filter, saved-search upsert, the dictionary's whole-word and
`key:value` guards, the kensaku import from both a JSON and a SQLite config,
the wallpaper route over a real socket (a bad data URL, an oversized upload, a
new extension replacing the old file, and a refusal that leaves the previous
wallpaper alone), the head script that paints it before the first frame, every
surface in the stylesheet being derived from `--hue` rather than pinned to a hex
value, and the two dictionaries against each other, against the markup and
against the kinds the query compiler can emit.

`tests/queries/queries.json` holds the fixed 20-query acceptance set from the
plan. Expected answers are still placeholders — attaching a real corpus is what
turns Recall@10 from a claim into a number.

## Not done yet

- `CLIP` and `e5` weights are not bundled; the installer budget is 378 MB
  against the plan's 150 MB, and that decision is still open.
- Index encryption, an app blocklist and a global OCR pause switch.
- Two interface languages. The dictionaries are the mechanism, not the
  coverage: a third language is a table and a line in `LANGUAGES`, but nobody
  has written one.
- A signed installer: the app window, tray, hotkey and shortcuts exist, but
  there is no MSI and no code signing yet, so "install" is a command rather than
  a wizard.
- Facet counts are computed for filter-only queries, not for ranked ones — the
  candidate pool is bounded, so the number would be a lie.
- OCR covers 66 languages on this machine; the rest of rapidocr's table
  (Hebrew, Bengali, Georgian, Armenian) needs a Windows language pack, which
  only the OS installer can add.
- Cold start is 0.77 s to a warm index, of which ~0.65 s is interpreter start
  and `import webview`. Getting under that needs a packaged binary, not faster
  Python.
- The document itself still has the inconsistencies listed in the review
  (top-10 table vs figure, p95 vs median, 30 Nov vs 29 Nov).

Closed since the kensaku comparison: the name index (1.7 ms at 100k instead of a
35 ms scan at 8k), the vector matrix (a memory-mapped pack: ~40 ms warm, ~0 ms
cold, no 333 MB allocation), version grouping, identical copies and screenshot
bursts, MCP over HTTP with `autofind connect`, OCR in 66 languages, cold start
under a second, and the launcher — a borderless window over the taskbar, a tray
icon, a global summon key and a login entry.

## License

MIT.
