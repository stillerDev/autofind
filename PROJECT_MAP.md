# PROJECT_MAP.md — карта проекта AutoFind

> Построена по результатам полного read-only исследования (ЭТАП 1) и профилирования на реальном
> индексе (`~/.autofind`, 21 643 файла, 313 041 чанк, схема v12). Все связи проверены по исходникам;
> неподтверждённое помечено `UNKNOWN`.

## 1. Что это

AutoFind — локальный поисковый движок по файлам и скриншотам на Python (Windows-first),
конкурент Rust-приложения kensaku. Гибридный поиск: нечёткий поиск по именам + BM25 + векторы,
фьюжн через RRF. Один SQLite-файл индекса в WAL-режиме + sidecar-файлы.

**Стек:** Python ≥3.11, stdlib (ThreadingHTTPServer — без FastAPI), SQLite (FTS5), numpy, Pillow,
опционально: PyMuPDF, ONNX Runtime (e5/CLIP/OCR), pywebview/pystray/pynput (десктоп), cryptography
(AES-256-GCM), rapidocr/winrt (OCR), tokenizers (статические эмбеддинги). Сборка — PyInstaller.

## 2. Общая архитектура

```mermaid
flowchart LR
    subgraph Пользовательские поверхности
        UI["web UI<br/>(index.html + app.js)<br/>браузер или pywebview-окно"]
        CLI["CLI: python run.py &lt;cmd&gt;"]
        MCP["MCP-клиенты<br/>(редакторы, агент)"]
        HOT["Глобальный хоткей<br/>ctrl+alt+space"]
    end

    subgraph Один процесс сервера
        API["core/api.py<br/>ThreadingHTTPServer 127.0.0.1"]
        HAND["Handler → _ReadStores pool (4)<br/>+ _WRITE_STORE singleton"]
        SRCH["core/search.py<br/>_rank: bm25 ∥ vector ∥ fuzzy → RRF"]
        EMB["core/embed/<br/>static-e5 (mmap-таблица)<br/>hashing (fallback)"]
        ST["core/store.py<br/>Store (SQLite v12)"]
        BM25["core/bm25.py<br/>InRamBM25 (CSR numpy)<br/>+ sidecars (.bm25seal / memmap)"]
        NIDX["core/nameindex.py<br/>именной индекс (bloom+subsequence)<br/>sidecar .nameidx"]
        VEC["core/filevec.py<br/>384-dim packbits, lut_scores"]
    end

    subgraph Фоновые службы
        IDXR["core/indexer.py<br/>IndexJob (одиночный)"]
        SCAN["core/scanner.py<br/>os.scandir + ignore.py"]
        EXT["core/extract/*<br/>ProcessPoolExecutor"]
        OCR["core/ocr/<br/>Windows OCR → rapidocr → indra"]
        WATCH["core/watch.py<br/>ReadDirectoryChangesW"]
    end

    UI -->|fetch /api/*| API
    CLI -->|локальный путь| SRCH
    CLI -.->|если сервер запущен<br/>делегация на сервер| API
    MCP -->|/mcp + Bearer токен| API
    HOT --> APP["core/app.py (_App)"]
    APP --> API
    API --> HAND --> SRCH
    SRCH --> EMB
    SRCH --> ST
    ST --> BM25
    ST --> NIDX
    ST --> VEC
    IDXR --> SCAN --> EXT --> OCR
    IDXR --> ST
    WATCH --> IDXR
```

## 3. Структура папок

| Путь | Назначение |
|---|---|
| `run.py` | Лаунчер + диспетчер CLI/приложения |
| `core/cli.py` | ~50 подкоманд CLI (index/search/serve/seal/backup/…) |
| `core/api.py` | HTTP-сервер, ~60 маршрутов, пул read-Store, write-Store |
| `core/search.py` | Гибридный поиск: 3 канала + RRF + бонусы kensaku |
| `core/store.py` | SQLite v12, file_texts (контентно-адресованные блобы), sealed-режим, sidecars |
| `core/bm25.py` | InRamBM25 — CSR-постинги numpy |
| `core/nameindex.py` | Именной индекс: bloom-префильтр, fzf-подобный скоринг, sidecar `.nameidx` |
| `core/filevec.py` | 48-байтовые packbits-векторы файлов, LUT-скоринг |
| `core/vectors.py` | Пак-бэкенд `.v4.vec` (с v9 фактически мёртв, см. аудит) |
| `core/indexer.py` | Конвейер индексации, ProcessPoolExecutor, heavy-lights-first |
| `core/scanner.py`, `core/ignore.py` | Обход диска, .gitignore-совместимые правила |
| `core/extract/` | Извлечение текста: plain/pdf/docx/office/delimited/archive/email/msg/dates |
| `core/ocr/` | 3 яруса OCR + indra (порт OCR kensaku) |
| `core/images.py` | Препроцессинг, миниатюры 320px, визуальные дескрипторы, phash |
| `core/chunker.py` | Нормализация + чанки (start,len)-спанами в единый текст документа |
| `core/privacy.py` | 7 категорий приватности (пути), фильтр до открытия файла |
| `core/vault.py` | AES-256-GCM, DPAPI или Argon2id/scrypt, режим seal |
| `core/query.py` | Парсер запроса: фильтры, CJK, regex, символы |
| `core/cjk.py` | Биграммы CJK для FTS и BM25 |
| `core/group.py` | Группировка версий документов и всплесков скриншотов |
| `core/personal.py` | Пины / сохранённые запросы / алиасы |
| `core/symbols.py` | Определения функций/классов regex-ами (без tree-sitter) |
| `core/watch.py`, `idle.py`, `lifespan.py` | Вотчер, idle-гейт, политика батареи |
| `core/mcp.py`, `connect.py`, `openapi.py` | MCP over stdio+HTTP, регистрация клиентов, OpenAPI-таблица |
| `core/memguard.py`, `maintenance.py`, `doctor.py`, `updater.py`, `transfer.py` | RAM-лимит, обслуживание, самопроверка, обновления, ZIP-перенос |
| `web/` | Фронтенд: index.html (961), app.js (3158), style.css (2020), i18n.js (1671), fon.jpg (95KB) |
| `tests/` | 48 тест-файлов, unittest discover |
| `tools/` | Дистилляция статических эмбеддингов, CLIP, иконки, coldstart-замеры |
| `docs/`, `BENCH.md`, `README*.md` | Документация и бенчмарки |

## 4. Карта API (основные маршруты, полный список в core/api.py)

```mermaid
flowchart TD
    subgraph GET
        S["/api/search — гибридный поиск (+CLIP fallback)"]
        ST2["/api/stats /status /progress /ready /health"]
        IMG["/api/thumb /preview /ocr-boxes /similar /images"]
        PERS["/api/pins /saved /aliases /tags /notes /collections /personal"]
        SYS["/api/watch /privacy /seal /log /self-test /audit /openapi.json"]
        EXP["/api/export?format=csv|json|md /duplicates/stream (SSE)"]
    end
    subgraph POST
        IDX["/api/index /reindex /reindex-one /delete"]
        P2["/api/pin /saved /alias /note /tag /collections /tag-color /hotkeys"]
        CFG["/api/privacy /seal /watch /background /onboarding /autostart /focus"]
        MCP2["/mcp (Streamable HTTP, Bearer)"]
    end
```

- Авторизация: bind 127.0.0.1 + проверка Host/Origin. REST **без токена** (осознанно);
  `/mcp` — Bearer из `<home>/mcp_token`.
- Threading: поток на запрос; read-Store из пула (≤4), запись через один write-Store;
  индексация — одиночный IndexJob; MemGuard сбрасывает кэши по RAM-лимиту.

## 5. Карта базы данных (schema v12)

```mermaid
erDiagram
    dirs ||--o{ file_rows : "dir_id"
    file_rows ||--o{ chunks : "file_id"
    file_rows }o--|| file_texts : "text_hash (контентный адрес)"
    file_rows ||--|| file_vectors : "file_id (48B packbits)"
    file_rows ||--o{ images : "file_id"
    file_rows ||--o{ tags : "file_id"
    file_rows ||--o{ notes : "file_id"
    file_rows }o--o{ collections : "collection_files"
    file_rows ||--o{ usage : "file_id (frecency)"
    file_rows ||--o{ pins : "file_id"
    images ||--o{ vectors : "owner_type='image'"
    chunks ||..o{ chunks_fts : "FTS5 contentless"
    symbols }o--|| file_rows : "file_id"
```

Ключевые таблицы: `dirs`(path UNIQUE), `file_rows`(basename+dir, char_mask, sha256, kind, mtime,
size, text_hash; индексы text_hash/mtime/size), `file_texts`(hash PK, zlib-блоки + offsets),
`chunks`(file_id, ord, start, len — спаны), `file_vectors`, `images`(thumb BLOB, ocr_text, phash),
`vectors`(int8+scale), `symbols`, `meta`, `recent_searches`, `pins/saved/aliases/tags/notes/
collections/tag_colors/usage`.

PRAGMA: `mmap_size=1GB`(writer)/0(ro), `cache_size` по роли, `journal_mode=WAL`,
`synchronous=NORMAL`, `busy_timeout=5000`, `wal_checkpoint(TRUNCATE)` на compact/close.

## 6. Поток данных поиска (горячий путь)

```mermaid
sequenceDiagram
    participant U as Пользователь
    participant F as web/app.js
    participant A as api.py Handler
    participant R as search._rank
    participant B as InRamBM25 (.bm25seal)
    participant N as nameindex (.nameidx)
    participant V as filevec lut_scores
    participant D as SQLite file_texts→chunks
    U->>F: ввод запроса
    F->>A: GET /api/search?q=…
    A->>R: search_page(store, embedder, q)
    par каналы параллельно
        R->>B: lexical (bm25_text, pool)
        R->>N: fuzzy_files (bloom+subsequence)
    end
    R->>V: encode_query → knn (gated для не-семантических эмбеддеров)
    R->>D: chunk_rows (сниппеты; для sealed — AES-GCM+zlib)
    R->>R: RRF-фьюжн + бонусы (имя/recency/frecency/pin/depth)
    R->>D: sha256_for → group_hits (версии/бёрсты)
    R-->>A: SearchPage(hits, facets, chips, hints)
    A-->>F: JSON
    F-->>U: таблица результатов
```

## 7. Поток данных индексации

```mermaid
flowchart LR
    R1["roots"] --> SCAN2["scanner.iter_entries<br/>(excluded_dirs + ignore)"] --> DEC{"size/mtime/<br/>sha-dup?"}
    DEC -->|новое| SORT["heavy-lights-first"] --> POOL["ProcessPoolExecutor<br/>extract (OCR/PDF/office)"] --> PRIV["privacy-фильтр"]
    DEC -->|без изменений| SKIP[skip]
    PRIV --> CH["chunker: спаны 1200/200"] --> INS["Store.replace_chunks:<br/>file_texts+chunks+vectors"]
    IMG["изображения: thumb+phash+visual"] --> INS
    INS --> COMMIT["commit каждые 1000"] --> BF["backfills: phash/symbols/doc_dates"]
    BF --> RS["sealed? → reseal_bm25()"]
```

## 8. Безопасность

- **HTTP**: только 127.0.0.1, Host/Origin; REST без токена (локальная модель угроз),
  `/mcp` — Bearer `mcp_token` (token_urlsafe(32), chmod 0600).
- **Seal (paranoid)**: AES-256-GCM (AFS1-блобы) над file_texts/ocr/thumb/symbols; FTS удаляется
  с диска; контентный поиск живёт через зашифрованный sidecar `.bm25seal`; ключ — DPAPI
  (`vault.key`, AFVK1) или passphrase (AFVK2, Argon2id/scrypt). Без ключа чтение падает с
  внятной ошибкой.
- **Приватность**: 7 категорий путей до открытия файла.
- **Обновления**: https-only + SHA256SUMS + лимит размера.
- Аудит `/api/audit` реализован целиком (`api.py:754-755,1052-1111`), включая `files`-view и `store_sealed`; пометка UNKNOWN снята.

## 9. Сборка/запуск/тесты

- Запуск из исходников: `python run.py` (окно) / `python run.py search …` (CLI) / `python run.py serve`.
- Из сборки: `dist/AutoFind/AutoFind.exe` (PyInstaller one-folder, console=False).
- CI: `.github/workflows/tests.yml` — unittest на windows-latest (3.11–3.13) + ubuntu smoke.
  Локально: `python -m unittest discover -s tests -t . -v` (pytest тоже работает).
- `pyproject.toml`: скрипт `autofind = core.cli:main`; extras: pdf/docx/onnx/watch/dev.

## 10. Основные пользовательские сценарии

1. **Мгновенный поиск** (хоткей/окно) → /api/search → гибридный рейтинг → группировка версий/бёрстов → открытие файла.
2. **Первый запуск**: onboarding → выбор папок → первая индексация (батарея/idle-гейты).
3. **Фоновая актуализация**: ReadDirectoryChangesW → отложенная доиндексация.
4. **Параноидальный режим**: seal → всё контентное шифруется, FTS падает, только .bm25seal.
5. **Переезд с kensaku**: `import kensaku` → пины/запросы/словарь переносятся.
6. **MCP-интеграция**: редактор подключается через `connect` → 6 read-only инструментов.
