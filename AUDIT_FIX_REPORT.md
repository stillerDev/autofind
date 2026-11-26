# AUDIT_FIX_REPORT.md — устранение находок из `AUDIT_REPORT.md`

Дата: 26.11.2026. Источник: `AUDIT_REPORT.md` (~190 позиций, 9 критичных). Работы велись пятью параллельными проходами по непересекающимся группам файлов плюс точечные правки вне их зон (`core/mcp.py`, `core/idle.py`, `core/embed/visual.py`, `models/manifest.json`, `shell-app/src/main.rs`).

## 1. Критичное — закрыто全部

| Где | Что было | Что сделано и почему |
|---|---|---|
| `core/cli.py`, `core/updater.py` | `check_update()` добывал SHA256, но не передавал его в `download_update()` / `apply_update()` — криптопроверка обновления была мёртва | Хэш現在 пробрасывается в обе функции; добавлена проверка `expected_size` после скачивания, независимо от `Content-Length`. Обновление не примет подменённый бинарник. |
| `core/watch.py:145-150` | Отказ сетевого корня делал `return` до `try/finally`, оставляя `state.running=True`; `watch_forever` крутился вечно | Перед возвратом выставляется `running=False`. Ворчер больше не висит без реального наблюдателя. |
| `core/indexer.py:372` | Любое исключение воркера обрывало индексацию до финального `store.commit()` | `future.result()` обёрнут: `BrokenExecutor` по-прежнему деградирует в последовательный режим, остальные ошибки идут в `stats.failed/errors`, `[done]` продолжается, коммит достигается всегда. |
| `core/query.py` | `negated` вычислялся, но только попадал в чип: `-type:pdf` фильтровал **на** PDF, `-re:"foo"` тоже | Добавлены поля `not_kinds/not_exts/not_in_paths/not_names/not_contents/not_regexes`; они участвуют в `matches()` и SQL-фильтрации. Отрицание теперь действительно исключает. |
| `core/extract/delimited.py`, `pdf.py`, `docx.py` | `read_bytes()` / `docx.Document()` читали весь файл в RAM, обходя бюджет | Бounded `open(...).read(max_bytes)`, дешёвая проверка ZIP-каталога до python-docx, `MAX_PDF_FALLBACK_BYTES = 8 MiB`, сохранение частичного текста при поздней ошибке OCR. OOM на большом экспорте устранён. |
| `core/ocr/__init__.py`, `engine.py` | Глобальный `_patience` без блокировки; параллельный OCR преждевременно отключал дорогой ярус | `threading.local()` + реестр с локом, `reset_fallbacks()` сбрасывает все потоки; lazy `_build()` получил double-check Lock. |
| `core/ocr/indra.py:377` | `mask = out[0] > 0.0` вместо задокументированных `> 0.5` — шум превращался в «линии» | Введён `CORE_THRESHOLD = 0.5`; ложный CJK-текст ушёл. |
| `core/personal.py:67` | `pattern.sub(expand, out)` трактовал путь `C:\work\cv` как regex → `bad escape \w` | Замена на `lambda _m: expand` — псевдонимы с путями и `\1` работают буквально. |
| `core/vault.py:165` | Любой текст с префиксом `AFS1` считался уже запечатанным и оставался открытым | Префикс больше не доверие: подлинность проверяется AEAD-тегом под текущим ключом; явный флаг `already_sealed=True` оставлен для осознанных вызывающих. |

## 2. Важное — по_area

**API и контракт** (`core/api.py`, `core/openapi.py`): Origin теперь сравнивается с портом реального сервера и требует точный `http://{Host}`; тело POST без `application/json` отклоняется (415); `start_server()` возвращает фактический порт, прогрев идёт туда же; `SEARCH_CACHE.clear()` под локом; пул READ_STORES ограничен счётчиком `in_use`; `_READER` закрывается в `finally`; `_replied` выставляется при каждой прямой записи; `_int_param()` пресекает `?limit=-1` и 500-е от `int()`; `/api/audit` считает через `COUNT(*)`, а не урезанные лимиты; `_default_roots()` знает локализованные имена и Known Folders; `Allow: POST` на GET `/mcp`; OpenAPI дополнил 11 маршрутов, исправил `id` вместо `path` и генерирует `responses` по кодам. Утечка `C:\Users\h1ile\Downloads` и даты `2026-10-25` из прогревочных запросов устранена — список evergreen-терминов и топ-5 последних запросов.

**Поиск и хранилище** (`core/query.py`, `search.py`, `store.py`, `nameindex.py`, `vectors.py`, `scanner.py`, `ignore.py`, `group.py`, `images.py`): `collection:A collection:B` теперь объединение; `_negative_text_ids()` реально исключает файлы по negated `re:`/`content:`; один `modified:` на запрос; `_data_cache_put()` после вытеснения всё ещё считает байты; `AUTOFIND_RAM_BM25` по умолчанию включён с порогом 1000 чанков; `vec0` и image-vector mirror включаются через `vector_backend`; sidecar-путь под `_BM25_LOCK`; `joined_offsets` больше не имеет системного off-by-one (`ALPHA_VERSION=3`), `scope_positions`/`prefix` клиппуют индексы; sketch-память паддится до 64 бит со счётчиками под локом; сканер индексирует dotfiles и вызывает `kind_for()`; ignore-правила регистронезависимые на Windows; копии, поглощённые version-группой, регистрируются в `by_copy`.

**Извлечение/эмбеддинги** (`core/extract/*`, `core/embed/*`, `core/bm25.py`, `core/chunker.py`): `BUDGET_KINDS` получил реальный `msg`-экстрактор; бинарные magic-байты больше не индексируются мусором; даты валидируются через `date()` и двухзначный год разворачивается корректно; ZIP-дескрипторы закрываются; `shared[index]` проверяет `0 <=`; EPUB продолжает после битой главы; RTF читает `\ansicpgNNNN`; пустой генератор эмбеддера не падает; выход ONNX выбирается по форме; static-таблица читает 16 байт вместо 96 МБ; GPU-провайдеры и имя входа берутся из сессии; кэш эмбеддеров под локом с `atexit`-закрытием; LRU сегментов BM25 вытесняет по-настоящему; CJK-пунктуация добавлена в границы предложений.

**CLI/вотчер/апдейтер/хранилище паролей** (`core/cli.py`, `watch.py`, `updater.py`, `vault.py`, `personal.py`, `doctor.py`, `app.py`, `run.py`): `verify` сравнивает size **и** mtime; корни индекса хранятся в `meta[index_roots]`, а не в `substr(path,1,3)`; `Store` закрывается в `finally`; `vector_backend` и CJK-параметры пробрасываются; `--hotkey` берётся из `hotkeys.json`; бэкап учитывает OneDrive/локализацию;(instance lock и порт освобождаются в `finally`; `first_run` пересчитывается при показе; `sys.frozen` заменён на `getattr`; `AutoFind.bat` уважает `AUTOFIND_HOME`; единый источник версии `core/__version__.py`; `pywin32` объявлен в зависимостях.

**OCR и разное**: thread-safe patience/движки, порог 0.5, закрытие WinRT-потоков, точный путь `tesseract`, Known Folder для Start Menu, `WinDLL(..., use_last_error=True)`, URI SQLite с квотированием, точное совпадение basename в `evaluate`, RFC-2822 даты, параметризованные spec-файлы, предупреждение о неизвестных категориях приватности, 10 тестов.

**MCP** (`core/mcp.py`, вне назначенных зон): батч JSON-RPC отвечает массивом, версия протокола согласуется с клиентом, курсоры круг洛夫о валидны, один сломанный tool не роняет stdio, общий Store защищён `RLock`.

**Окно уведомления** (`shell-app/src/main.rs`, `notify.html`): всплывашка больше не мелькает по центру — создаётся скрытой и прозрачной, переезжает к иконке трея через `MoveWindow` и только затем показывается; серый дизайн, настоящая иконка 30×30, без белых краёв, зелёный бейдж/прогресс, автоскрытие 2,5 с.

## 3. Что осталось и почему

- **Централизация ~80 магических чисел в `core/constants.py`** — самый большой пункт §4. Часть уже именованные константы на местах; единый модуль потребует правок почти всех 68 файлов и отдельного прохода с проверкой регрессий. Не сделано в этом батче.
- `core/config.py:23` — `os.environ.setdefault("AUTOFIND_EMBEDDER", "hashing")`: удаление требует согласованной смены дефолта в `embed/__init__` и CLI; оставлено, чтобы не сменить бэкенд у существующих индексов.
- `models/manifest.json` — `sha256: null` у `e5-small`/`clip-vit-b32`: вымышленные хэши писать нельзя. Нота манифеста исправлена: проверка REAL-загрузок живёт в `core/models.py::PACKS` (там хэши есть), `null` означает «только в инсталлере, нет опубликованного экспорта», а не «проверено».
- Web-панель «Шифрование» не получила кнопку passphrase (CLI `vault passphrase` добавлена); правка в `web/` выходит за пределы аудиторского батча.
- `core/pdf.py::_ocr_page_cap()` всё ещё берёт первый доступный OCR-движок, а реальный выбирается позже — требует изменения `core/ocr/__init__.py::_first_pass`.
- Таблицы `ocr/languages.py` и константы `ocr/indra.py` не вынесены в JSON-манифесты: нужны новые файлы данных и миграция тестов.
- MCP HTTP всё ещё делит один `McpServer` между потоками — теперь вызовы сериализованы локом и в `api.py`, и в `mcp.py`; полноценный store-per-request — отдельная задача.
- Линтер `ruff E9,F` по `core/` — чисто. По `tests/` осталось 22 старых F401/F841 в файлах, не затронутых аудитом; это не продакшен-код.

## 4. Проверка

- `python -m unittest discover -s tests -t .` → **683 tests, OK**.
- `python -m ruff check core --select E9,F` → **All checks passed**.
- `python -m compileall -q core run.py tools tests` → exit 0 (у аудитора и после правок).
- `cargo build --release` (shell-app) → успешно; уведомление проверено: центр экрана остаётся тёмным на кадрах 0.2–0.6 с, карточка появляется у трея и исчезает через 2,5 с.
- Живой сервер после перезапуска: `browser extension` → `BROWSER_EXTENSION.md` первым; `-type:pdf test` больше не фильтрует на PDF; `/api/search` отвечает 200.

Новые регрессионные модули: `tests/test_audit_api.py`, `test_audit_cli.py`, `test_audit_extract.py`, `test_audit_search.py`, `test_audit_misc.py`, `test_audit_mcp.py`.
