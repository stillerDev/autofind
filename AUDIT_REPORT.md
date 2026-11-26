# AUDIT_REPORT.md — свежий аудит AutoFind (26.11.2026)

**Методика.** Прочитана `PROJECT_MAP.md` (карта), затем полный перечень файлов проекта, затем исходники: `core/` — 68 `.py` / 25 879 строк, `web/` — 4 файла / 7 880 строк, `tests/` — 58 файлов, плюс `run.py`, `native/`, `*.spec`, `AutoFind.bat`, `pyproject.toml`, `requirements.txt`, `models/manifest.json`, `.github/workflows/tests.yml`. Параллельно работали 8 аудиторских проходов по группам файлов. Все **критичные** и часть важных находок перечитаны вручную и помечены `✓`.

**Проверки, прошедшие:** `python -m compileall -q core run.py tools tests` → exit 0. Голых `except:` и `assert` в продакшен-коде нет. Skip-тесты все условные (окружение), заглушек `assert True` / `if False` нет.

**Статистика находок (без учёта хардкода-мелочей):** ~190 позиций. Из них критичных — 9, важных — ~70.

---

## 1. КРИТИЧНО (чинится в первую очередь)

### 1.1. Обновление не проверяет SHA256 — криптопроверка мёртва `✓`
`core/cli.py:393-394` + `core/updater.py:179,221,262,283`
`check_update()` кладёт хэш в `info["sha256"]`, но `download_update(info["url"], info.get("size", 0))` и `apply_update(staged)` вызываются без него; обе проверки внутри (`if expected_sha256:`) пропускаются.
**Замена:**
```python
staged = download_update(info["url"], info.get("size", 0), info.get("sha256"))
apply_update(staged, expected_sha256=info.get("sha256"))
```

### 1.2. Вотчер зависает при отказе сетевого корня `✓`
`core/watch.py:145-150` (см. `start()`:127, `watch_forever`:518)
`return` происходит до `try/finally` (153-179), а `state.running = True` уже выставлен → цикл `while watcher.state.running` крутится вечно без вотчера.
**Замена:** `self.state.running = False` перед `return`, либо перенести проверку в `try` с общим `finally`.

### 1.3. Исключение воркера обрывает всю индексацию без коммита `✓`
`core/indexer.py:372` (обработчик только на `BrokenExecutor` — строка 422)
`future.result()` перевыбрасывает любое исключение парсера/эмбеддера; внешний `try` не ловит его → run падает до финального `store.commit()` (247).
**Замена:** обернуть `future.result()` в `try/except Exception`, инкрементировать `stats.failed`, записать в `stats.errors`, `continue`; `BrokenExecutor` оставить отдельной веткой.

### 1.4. Отрицание фильтров не работает — `-type:pdf` фильтрует НА pdf `✓`
`core/query.py:488-489, 477-479, 491-643`
`negated` вычисляется, но идёт только в строку `literal` для чипа; ни одна ветка `_apply_key` его не использует. `-re:"foo"` тоже кладётся в `query.regexes` как положительный.
**Замена:**
```python
# в начале _apply_key:
if negated:
    query.not_paths.append(literal.lstrip("-"))
    query.chips.append(Chip("exclude", literal, literal, literal))
    return
# для regex в parse():
if token.negated: query.not_paths.append(token.text)
else: query.regexes.append(token.text)
```
Дополнительно `_has_explicit_filter` (809) видит key и отключает intent-компиляцию — учесть и там.

### 1.5. Чтение всего файла в RAM в обход заявленных бюджетов `✓`
- `core/extract/delimited.py:133` — `Path(path).read_bytes()[:max_bytes]` читает 10 ГБ экспорт целиком. **Замена:** `with open(path,"rb") as fh: data = fh.read(max_bytes)`.
- `core/extract/pdf.py:184` — `_minimal_text(Path(path).read_bytes())` без лимита. **Замена:** `f.read(min(max_bytes, MAX_PDF_FALLBACK_BYTES))`.
- `core/extract/docx.py:36-41` — `docx.Document(str(path))` парсит весь OPC-пакет до бюджетов; защищённый `_extract_via_zip` только fallback. **Замена:** сначала дешёвая проверка `zipfile` на размер/число записей, либо сразу `_extract_via_zip`, а python-docx — только для файлов меньше порога.

### 1.6. Гонка глобального OCR-счётчика отключает дорогой ярус `✓`
`core/ocr/__init__.py:91` + `engine.py:29` (параллелизм по изображениям)
`_patience = _Patience()` — один mutable-счётчик на процесс, `note()`/`spent()` вызываются из потоков без синхронизации → преждевременный `GIVE_UP_AFTER` и тихий отказ OCR.
**Замена:** `threading.local()` для `_patience` (или `threading.Lock` вокруг `note/spent`), сброс во всех потоках в `reset_fallbacks()`.

### 1.7. Порог детектора линий в 8 раз ниже задокументированного `✓`
`core/ocr/indra.py:377` (докстринг 370: `p > 0.5`)
`mask = out[0] > 0.0` — шумовые logits становятся «линиями», раздувают `count`, дают ложный CJK-текст.
**Замена:** `CORE_THRESHOLD = 0.5` рядом с `OUT_STRIDE` (316), `mask = out[0] > CORE_THRESHOLD`.

### 1.8. Псевдоним с путём ломает парсер запроса `✓`
`core/personal.py:67` — `pattern.sub(expand, out)` трактует `C:\work\cv` как шаблон re → `bad escape \w`; `\1` → `invalid group reference`.
**Замена:** `out = pattern.sub(lambda _m: expand, out)`.

### 1.9. Произвольный текст с префиксом `AFS1` остаётся открытым в paranoid-режиме `✓`
`core/vault.py:165-167` — `seal()` возвращает данные как есть, если они начинаются с `MAGIC`.
**Замена:** убрать ранний `return data`; повторное запечатывание отличать по отдельному флагу, а не по префиксу контента.

---

## 2.ВАЖНЫЕ БАГИ

### 2.1. HTTP / API (`core/api.py`)

| Где | Проблема | Замена |
|---|---|---|
| `612-616` `✓` | Origin сравнивает только hostname; `http://localhost:8080` проходит как свой → CSRF на `/api/delete`, `/api/seal`, `/api/pin` (JSON парсится независимо от Content-Type, 2154) | требовать `origin == f"http://{Host}"` либо сравнивать порт с `self.server.server_address[1]`; принимать только `Content-Type: application/json` |
| `2569` + `2433-2437` `✓` | `start_server` переприсваивает `port` при занятом 8765, а `serve()` передаёт в прогрев своё неизменное значение → прогрев молча идёт не туда | возвращать выбранный порт из `start_server` (`server.server_address[1]`) и передавать его в `serve()` / `_prewarm_query_cache` |
| `226-228` `✓` | `clear_search_cache()` делает `.clear()` без `SEARCH_LOCK`, пока `prune_search_cache` итерирует под блокировкой → `RuntimeError: dictionary changed size` | `with SEARCH_LOCK: SEARCH_CACHE.clear()` |
| `263-274` `✓` | `_limit=4` ограничивает только возвращённые соединения; число выданных ничем не ограничено — 100 параллельных запросов откроют 100 Store. Комментарий «Four connections is plenty» не соответствует коду | учитывать выданные через счётчик/семфор: создавать новый только если `in_use + len(_free) < _limit` |
| `2644` + `2597-2602` `✓` | `_READER` объявлен «held, never closed», но `serve().finally` закрывает только `READ_STORES`, MCP и сервер → на Windows индекс остаётся заблокирован | `if _READER is not None: _READER.close()` в `finally` |
| `2236-2249` + `mcp.py:187,197-203` | один `McpServer` и его единственный `_store` разделяются между потоками; `check_same_thread=False` убирает ошибку потока, не гонку | пул Store для MCP или `McpServer` на запрос; минимум — общий лок вокруг вызовов инструментов |
| `2261-2281, 1046-1050, 823-828, 1360-1369` | обработчики пишут в `wfile` напрямую, не выставляя `_replied`; при исключении — второй ответ на keep-alive | выставлять `self._replied = True` сразу после `end_headers()` или перейти на общий `_send_raw` |
| `1635` `✓` | `limit = min(int(...), 200)` без нижней границы; `?limit=-1` → SQL `LIMIT -1` = без лимита, `/api/symbols` отдаёт всю таблицу | `max(1, min(int(...), 200))` |
| `2303-2309` | `allow_remote` из payload не участвует в сравнении при переиспользовании Watcher | включить в условие 2306-2307 и сохранить атрибутом |
| `2343, 2366, 1926` | `/api/delete`, `/api/privacy/purge`, `/api/seal` пишут в индекс без проверки `JOB.running` (в отличие от `_kensaku_import`) → `database is locked` / битый WAL | проверять `JOB.running`, возвращать 409; перед seal — `evict_caches()` |
| `1806, 1844, 2006, 2062, 1124-1125` | `int()` на query-параметрах без обработки `ValueError` → 500 с текстом исключения вместо 400 | хелпер `_int_param(query, name, default, lo, hi)` |
| `1898-1903` | `_seal_status()` читает `_SEAL` без `_SEAL_LOCK`, хотя воркер пишет под ним | копировать `dict(_SEAL)` внутри `with _SEAL_LOCK` |
| `754-755, 1052-1111` `✓` | `/api/audit` реализован (PROJECT_MAP:202 помечает UNKNOWN — пометка устарела), но считает `all_tags(limit=1000)` и `indexed_sensitive(limit=10000)` — число занижено | считать через `COUNT(*)` или вернуть `truncated: true`; поправить PROJECT_MAP |
| `2391-2399` | `_default_roots()` только англоязычные имена; на русском Windows (`Документы`, `Загрузки`) возвращает пустой список | Known Folders API (`SHGetKnownFolderPath`) или локализованные имена из конфига |
| `666-672` | GET `/mcp` → 405 без обязательного заголовка `Allow: POST` | добавить `Allow` в ручной ответ |

### 2.2. Поиск, индекс, CLI

| Где | Проблема | Замена |
|---|---|---|
| `search.py:495-518` `✓` | Докстринг обещает union для `collection:Work collection:Home`, код делает `scope & places` → пустой результат | `scope = places if scope is None else scope \| places` (для тегов оставить `&`) |
| `mcp.py:435-446` | stdio `run()` не обрабатывает JSON-RPC batch и не ловит исключения инструментов → падение цикла | проходить по элементам массива; обернуть `handle` в `try` с `-32603`, как в `api.py:2243-2255` |
| `mcp.py:28,210-214` | `PROTOCOL_VERSION = "2024-11-05"` всегда, без переговоров | брать версию клиента, если поддерживается |
| `mcp.py:42-43` | `_encode_cursor` не ограничивает offset, `_decode_cursor` принимает только `o(\d{1,7})` → сервер генерирует нечитаемый курсор | убрать `{1,7}` или проверять в encode |
| `indexer.py:762` `✓` | `_extract_image_job` читает `_WORKER_CONFIG.db_path` (глобал инициализируется только в воркерах, 575-582); в main-процессе `None` → retry из 388 всегда failed | `twin = _worker_twin_image(config.db_path, digest, phash)` |
| `indexer.py:712,721,799,978,1030` | `chunk_spans()` нормализует текст внутри (`chunker.py:34`), а срезы для эмбеддера берутся из исходного → CRLF/пробелы сдвигают текст | `norm = normalize(text); chunks = [norm[a:b] for a,b in spans]`, `packed` тоже из `norm` |
| `indexer.py:226,470,485` | `enumerate(todo)` без `start=1`, `done = done_before + position` → прогресс останавливается на N-1 | `enumerate(todo, start=1)` и убрать `+1` |
| `indexer.py:613-614` | докстринг обещает `AUTOFIND_NO_THREADS=1`, код читает `AUTOFIND_THREAD_POOL` → переключатель не работает | привести имена к одному |
| `cli.py:1343-1345` `✓` | `SELECT DISTINCT substr(path,1,3)` — на Windows всегда `C:\`, на POSIX обломок `/ho` | хранить корни в отдельной таблице/мета и читать оттуда |
| `cli.py:1768,1786` | `Store(..., vector_backend="pack")` игнорирует глобальный `--vector-backend` → seal/unseal несовместимым бэкендом | `vector_backend=config.vector_backend` |
| `cli.py:1488` `✓` | `verify` сравнивает только `st_size`, хотя индексатор считает изменения по size **и** mtime | `if st.st_size != int(row["size"] or 0) or abs(st.st_mtime - float(row["mtime"])) >= 1e-6: changed += 1` |
| `cli.py:1562-1585,1608-1631` | `_cmd_saved`/`_cmd_alias` закрывают Store вручную в каждой ветке, без `finally` → утечка при любом другом исключении | единый `finally: store.close()` |
| `cli.py:1024` | `ocr_image(...)` без CJK-параметров, которые индексатор передаёт в 783-788 → `autofind ocr read` читает иначе, чем индекс | передать `cjk=config.ocr_cjk, cjk_model=..., cjk_keys=...` |
| `scanner.py:246` `✓` | `if name.startswith("."): continue` отсекает `.env`, `.bashrc`, `.gitattributes`, `.dockerignore` хотя они есть в `TEXT_EXT` | пропускать только служебные имена: `if name in IGNORE_FILES: continue` |
| `scanner.py:263-268` `✓` | при обходе неизвестное расширение сразу `kind="other"` без `kind_for()`; однострочный путь (305) сниффит → один файл индексируется по-разному | `kind = kind_for(path)` |
| `ignore.py:59,93-95,121` | на Windows `case_insensitive=True` и ключи в нижнем регистре, но regex компилируется из исходного паттерна → `Build/` не матчит `build` | `re.compile(..., re.IGNORECASE)` в `_glob_to_regex` |
| `chunker.py:17` | `SENTENCE_BOUNDARY` только ASCII; CJK-пунктуация отсутствует → китайский текст не разбивается по предложениям | добавить class: `(?<=[.!?…;:;。！？；：])\s+\|\n{2,}` |
| `query.py:477-479` `✓` | `-re:"foo"` — negated флаг теряется, фильтрует на foo | см. 1.4 |
| `query.py:765-770` | цикл `_TIME_RULES` без `break` → два `modified:`-фильтра, первый чип врёт | `break` после первого совпадения (как `_SORT_RULES`:763) |
| `group.py:204-208,220` | `by_copy[...]`.register только при создании новой группы; хит, поглощённый `by_version`, не регистрируется | `by_copy.setdefault(copy_key, existing)` в ветке `by_version` до `continue` |

### 2.3. Хранилище и индексы (`core/store.py`, `vectors.py`, `nameindex.py`)

| Где | Проблема | Замена |
|---|---|---|
| `store.py:101-103` `✓` | `_data_cache_put` при превышении числа записей делает `popitem(); return` и не выполняет байтовый учёт (106-111) → после 8192 записей лимит 12 MB мёртв, память растёт | убрать `return` на 103 |
| `store.py:134` `✓` | `AUTOFIND_RAM_BM25` по умолчанию `"0"` — in-RAM BM25 выключен везде, хотя комментарий 127-133 утверждает обратное | дефолт `"1"` или поправить комментарий |
| `store.py:541-565,1245,1315-1323` `✓` | `prepare_vectors()` не вызывается; `_requested_backend` пишется и не читается; `_setup_vectors()` всегда `"filebin"` → опция `--vector-backend sqlite-vec` мёртва и для image-векторов (докстринг 1321 неверен) | вызывать `prepare_vectors` при `sqlite-vec`, либо удалить опцию и мёртвый код |
| `store.py:2088-2109` | однопоточная ветка `ram_bm25=False` работает с глобальным `_BM25` без `_BM25_LOCK`, хотя вторая ветка (2120+) под блокировкой; `api.py:648` и `1675` используют оба пути одновременно | обернуть 2088-2109 в `with _BM25_LOCK` |
| `vectors.py:399-406` | `row_u64` шириной `ceil(dim/64)*8`, `qb` — `ceil(dim/8)`; при dim не кратном 64 `.view(np.uint64)` падает | паддить: `np.pad(qb, (0, sketch_row - qb.size))` |
| `vectors.py:415,426-439` | счётчики `_sketch_tries/_sketch_accepts` — обычные атрибуты общего Pack без блокировки → ложное отключение popcount-оптимизации | `threading.local()` или лок |
| `nameindex.py:169-180` `✓` | `joined_offsets` отводит `length+1` на каждый элемент, `b"\x00".join` добавляет только `count-1` → `offsets[-1] = blob_len + 1`, системный off-by-one | `starts[1:] = np.cumsum(lengths)` + завершающий NUL в blob, либо убрать `+1` у последнего |
| `nameindex.py:760-772,806-817` `✓` | `scope_positions`/`prefix()` индексируют массив по всем записям до наложения маски `ok` → `IndexError` на длинном префиксе (`in:` запрос и автодополнение) | клипповать индексы `np.minimum(..., len(array)-1)` и накладывать `ok` до чтения |
| `nameindex.py:48` | `MAX_ENTRIES = 1_500_000` с комментарием «fallback to SQL» ни разу не используется → 1.5M+ записей строятся в RAM | проверять `_row_count(store)` в `_build()` (937-949) или удалить константу |

### 2.4. Извлечение, эмбеддинги, OCR

| Где | Проблема | Замена |
|---|---|---|
| `extract/__init__.py:44` `✓` | `BUDGET_KINDS` содержит `"msg"`, но в `EXTRACTORS` (28-40) такого ключа нет → `kind="msg"` молча возвращает `""` | добавить `"msg": _extract_email_dispatch` или убрать из `BUDGET_KINDS` |
| `extract/archive.py:90-96` | `archive.extractfile(info)` не закрывается → до 400 утечек объектов на tar | `with archive.extractfile(info) as handle:` |
| `extract/dates.py:55-56` | двузначный год всегда `+2000`; `05.03.95` → 2095 и отбрасывается → даты 1990-х теряются | `year += 1900 if year >= (date.today().year % 100) + 20 else 2000` |
| `extract/dates.py:37` | `_valid` проверяет только `1<=day<=31`; `31.02.2026` валидна | `try: date(year,month,day) ... except ValueError: return False` |
| `extract/msgoutlook.py:25` | `msg.close()` только на успешном пути | `try/finally: msg.close()` |
| `extract/office.py:209` | `shared[index] if index < len(shared)` — отрицательный индекс проходит: `<v>-1</v>` даёт чужую строку с конца | `0 <= index < len(shared)` |
| `extract/office.py:167,201` | `lines` общий для всех листов; первый лист на 20K строк блокирует остальные | счётчик `rows_on_sheet` на лист |
| `extract/office.py:268` | `except _DEATHS: break` в цикле глав EPUB — один битый chapter обрывает все остальные, хотя комментарий 134-136 обещает обратное | `continue` (break только для `BudgetExhausted`) |
| `extract/office.py:304` | RTF hex всегда `cp1251`, игнорируется `\ansicpg1252` → иероглифы | парсить `\ansicpg(\d+)` и `codecs.lookup(f"cp{code}")` |
| `extract/pdf.py:167-168` | `except Exception: pass` выбрасывает уже собранный `full_text` (включая `SCANNED_MARKER`) при любой поздней ошибке | сохранить `partial = full_text` и возвращать его из fallback |
| `embed/hashing.py:36` | `if texts` истинен для пустого генератора → `np.vstack([])` падает | `texts = list(texts); ... if texts else np.zeros((0, DIM), np.float32)` |
| `embed/onnx_e5.py:99` | `session.run(None, feeds)[0]` предполагает первый выход = hidden states; в `visual.py:166-179` та же проблема решена поиском по форме | выбирать 3-мерный выход перебором `get_outputs()` |
| `embed/static.py:63` | `table_path.read_bytes()[:HEADER.size]` читает весь 96 МБ файл ради 16 байт → убивает главное преимущество mmap | `with open(...) as fh: fh.read(HEADER.size)` |
| `embed/static.py:64` | `HEADER.unpack` на битом файле бросает `struct.error`, который не является `StaticUnavailable` → падение вместо fallback | проверять `len(raw) != HEADER.size` и оборачивать `struct.error` |
| `embed/visual.py:126,134` | обе сессии CREATE с `providers=["CPUExecutionProvider"]`, игнорируя `devices.gpu_provider()` | `providers=session_providers()` с fallback как в `onnx_e5.py:59-68` |
| `embed/__init__.py:18,58-60,81` | `_EMBEDDER_CACHE` — dict без блокировки; параллельные первые вызовы строят по несколько 96 МБ эмбеддеров, никто не вызывает `close()` | `threading.Lock` + double-check + реестр с `close()` на shutdown |
| `ocr/engine.py:337-339,391-392` | `if self._engine is None: self._engine = self._build()` без блокировки в двух rapidocr-движках; в `indra.py:1196-1219` для той же задачи есть Lock | `self._lock = threading.Lock()` + double-check |
| `ocr/engine.py:178-186` | WinRT `InMemoryRandomAccessStream` / `DataWriter` не закрываются | `try/finally: writer.close(); stream.close()` |
| `ocr/engine.py:410,416` | `available()` ищет `shutil.which("tesseract")`, а `subprocess.run` вызывает литерал `"tesseract"` | сохранить `self._exe` и использовать его |
| `ocr/engine.py:278-283,548-551` | `available()`RapidOcrMulti возвращает `self._error is None`, а `_error` ставится только после неудачного `recognize()` → опечатка в `--ocr-lang` принимается | проверять наличие файла модели / валидировать против `languages.MODELS` |
| `ocr/indra.py:964-976` | `spec.vocabs[""]` прямое обращение → `KeyError` на meta.json без ключа, `_core()` навсегда отключает Indra | `base = spec.vocabs.get(""); if base is None: raise ValueError(...)` |
| `ocr/indra.py:1098-1099` | `src = tall ...`, а `widths` считается по `crops` → лишний padding и риск `MAX_AREA` | `widths = [src[j].w for j in lines]` |

### 2.5. Разное (`updater`, `watch`, `doctor`, `app`, конфиги)

| Где | Проблема | Замена |
|---|---|---|
| `updater.py:293` `✓` | `if sys.frozen:` — атрибута нет в обычном CPython → необработанный `AttributeError` вне PyInstaller | `if getattr(sys, "frozen", False):` |
| `updater.py:195,213-214` | `expected_size` из GitHub игнорируется, если сервер отдал `Content-Length` | дополнительно `if expected_size and downloaded != expected_size: raise UpdateError` |
| `watch.py:408` | склейка через `"\\"`: root `C:/docs` → `C:/docs\file.txt`, пути не совпадают с индексом | `str(Path(root) / name)` |
| `doctor.py:146` | `self_test` берёт модели из `_default_home()`, игнорируя переданный `db_path`/`config.home` → ложный «cannot open» при кастомном `AUTOFIND_HOME` | передавать `model_dir=config.model_dir` |
| `doctor.py:70-87` + `models/manifest.json:13,33,45,55` | `estimate_bundle` складывает 120+280+22+100 МБ и сравнивает с `BUDGET_MB=150` → `fits` всегда False | считать только реально бандленные модели, загружаемые — отдельной строкой |
| `app.py:483,581-595` | `first_run` вычисляется один раз; при `--hidden` welcome не появляется при первом показе окна | пересчитывать в `show()`: `if not onboarding_done(self.home): self.first_run = True` |
| `app.py:811-845` | лок захвачен в 811, `try/finally` начинается только в 845 → при исключении лок и порт не сбрасываются | обернуть весь блок в `try/finally` |
| `AutoFind.bat:14,21,26` `✓` | жёстко `%USERPROFILE%\.autofind`, приложение читает `AUTOFIND_HOME` → второй запуск не поднимет окно | `if defined AUTOFIND_HOME (set "HOME=%AUTOFIND_HOME%") else (set "HOME=%USERPROFILE%\.autofind")` |
| `models/manifest.json:3,16,36` `✓` | заметка обещает sha256-пининг, но у e5-small и clip-vit-b32 `"sha256": null` | заполнить хэши файлов и проверять в загрузчике |
| `requirements.txt` + `pyproject.toml:12` + `shortcut.py:74` | `win32com.client` импортируется, но `pywin32` не объявлен → `shortcut` падает на чистой установке | `pywin32>=306; sys_platform == "win32"` |
| `AutoFind-debug.spec:9` | `collect_data_files('rapidocr_onnxruntime')` при зависимостях на `rapidocr` → debug-сборка теряет yaml/модели | `collect_data_files('rapidocr')` |
| Версии | `core/__version__.py:1` = `1.0.0`, `core/__init__.py:3` = `0.1.0`, `pyproject.toml:7` = `0.1.0`, `doctor.py:247` = `0.1.0`, `api.py:585` = `"AutoFind/0.1"` `✓` | единый источник: импортировать `__version__` и подставлять везде |
| `images.py:39,71-72,88` | `sha256_bytes() -> str`, но возвращает `bytes`; `ImageInfo.sha256` получает сырые 32 байта | аннотация `-> bytes` и поле `digest`, либо `hexdigest()` |
| `vault.py:44-48` | комментарий «256 MiB» для scrypt; реально `128*131072*8 = 128 MiB` | поправить комментарий или поднять `n` до `2**18` |
| `config.py:136-140` | неизвестные категории из `privacy.json` молча отбрасываются → опечатка тихо отключает защиту | логировать предупреждение / оставлять дефолт |
| `transfer.py:73`, `migrate.py:267` | URI SQLite из сырого пути без квотирования → путь с `#`/`?` обрезается | `Path(db_path).resolve().as_uri() + "?mode=ro"` |
| `maintenance.py:123-136` | `WinDLL("psapi")` без `use_last_error=True` → `WinError` всегда показывает 0 | `WinDLL("psapi", use_last_error=True)` |
| `maintenance.py:176-183` | параметр `home` в `_update_temp_dirs(home)` не используется | убрать параметр и поправить вызовы (214,328) |
| `evaluate.py:111-116` | `if needle in lowered` — подстроковое совпадение завышает recall (`report.doc` найден в `myreport.docx`) | сравнивать точный basename |
| `importers.py:63-73` | `parsedate_to_datetime` импортирован с `# noqa: F401`, но не используется — RFC-2822 даты дают `mtime=0.0` | добавить ветку `parsedate_to_datetime(raw_time).timestamp()` |
| `shortcut.py:97-105` | `remove()` не проверяет `supported()`, в отличие от `create()` | добавить ту же проверку |
| `doctor.py:38-43` | `_dir_size` не ловит `OSError` → один недоступный файл роняет doctor | `try/except OSError` и продолжить |

---

## 3. НЕДОРАБОТКИ И РАСХОЖДЕНИЯ С ДОКУМЕНТАЦИЕЙ

| Где | Что заявлено / что реально | Замена |
|---|---|---|
| `core/api.py:2487-2533` `✓` | `_prewarm_query_cache` — список из ~30 демо-запросов, дата `2026-10-25`, **путь `C:\Users\h1ile\Downloads`** (2523) и 35+ HTTP-запросов на каждый старт сервера. Это ещё и утечка персональных данных в репозиторий плюс лишняя нагрузка на старте | вынести список/дату в конфиг; путь брать из `_default_roots()`/`config`; сделать прогрев включаемым и ограниченным (топ-5 последних запросов) |
| `core/api.py:2391-2399` | `_default_roots()` только английские имена | Known Folders API |
| `core/api.py:754-755,1052-1111` `✓` | PROJECT_MAP:202 помечает `/api/audit` как UNKNOWN — он реализован, но с обрезающими лимитами | снять UNKNOWN, убрать лимиты |
| `core/openapi.py:20-76` `✓` | в документе нет 9 реализованных маршрутов: GET `/api/self-test`, `/api/health`, `/api/notes`, `/api/tag-colors`, `/api/duplicates/stream` (api.py:728,730,746,748,766) и POST `/api/index`, `/api/reindex-one`, `/api/note`, `/api/tag-color` (2159,2195,2197,2199). Главный `/api/index` не описан вообще | добавить в `ROUTES`; генерировать таблицу из единого реестра |
| `core/openapi.py:49-50` `✓` | документ объявляет query `path` для `/api/open` и `/api/reveal`, обработчик читает `id` (api.py:2062) → клиент по OpenAPI всегда получит 404 | заменить `["path"]` на `["id"]` |
| `core/openapi.py:53` | POST `/api/reindex` описан с полем `roots`, но `_reindex()` не принимает payload | убрать `roots` из документа или реализовать приоритет `payload["roots"]` |
| `core/openapi.py:67` | POST `/api/collections` описан как `["name","paths"]`, обработчик ждёт `action,id,ids,name` | заменить поля |
| `core/openapi.py:87-89` | для всех маршрутов описан только `200`, хотя код возвращает 202/401/403/404/409/413/500/503 | генерировать `responses` из таблицы кодов |
| `core/openapi.py:17` | `SERVER_URL = "http://127.0.0.1:8765"` мёртвая константа | удалить или сделать источником `DEFAULT_PORT` |
| `core/indexer.py:624-625` | провал `_open_pool` молча превращается в sequential без записи в `stats.errors` | `stats.errors.append(("", f"pool unavailable: {type(exc).__name__}: {exc}"))` |
| `core/cli.py:1444` | подкоманда `dead` объявляет `--limit` (217), но `finders.dead_paths()` вызывается без него | передавать `limit=args.limit` или убрать флаг |
| `core/cli.py:1205` | бэкап по умолчанию в `Path.home()/"Desktop"` — не учитывает OneDrive/локализацию | Known Folder API или обязательный `--to` |
| `core/ocr/engine.py:502-506,525,539-544` | докстринг обещает подключать отсутствующие модели «когда ничего не осталось», но `add()` требует `model in installed` и исключает их | отдельный проход с `allow_download=True` |
| `core/extract/plain.py:8` | `BINARY_MARKERS` объявлен и не используется; JPEG/PNG без NUL в первых 4 КБ проходят `latin-1` и индексируются мусором | `if data.startswith(BINARY_MARKERS): return ""` |
| `core/extract/pdf.py:58-71` | `_ocr_page_cap()` берёт `available_engines()[0]`, а реальный движок выбирается в `ocr/__init__.py:_first_pass` — кап может не соответствовать движку | передавать имя выбранного движка |
| `core/extract/__init__.py:83-87` | два последовательных `except`, оба возвращают `""`; широкий маскирует programming errors | один `except Exception as exc` с `logger.debug(...)` |
| `core/embed/visual.py:144-145` | `feed = {"pixel_values": tensor}`; если экспорт объявляет другое имя, `_accepted` отфильтрует всё и `session.run(None, {})` упадёт | имя из `session.get_inputs()[0].name` |
| `core/bm25.py:342-367` | `_SegLRU.put` имеет `while ... and len(self._items) > 1` — последний сегмент >16 МБ никогда не вытесняется, хотя докстринг обещает bounded | `while self._bytes > self._max and self._items:` (кроме только что положенного) |
| `core/group.py:99,121` | докстринг обещает 64-bit dhash, возврат маскируется `& ((1<<63)-1)` — реально 63 бита | поправить докстринг: «63-bit, top bit dropped for signed SQLite INTEGER» |
| `core/maintenance.py:176-183` | параметр `home` не используется, докстринг уверяет обратное | убрать параметр |
| `core/importers.py:68` | импорт `parsedate_to_datetime` с noqa, но RFC-даты не парсятся | добавить ветку |
| `core/config.py:136-140` | неизвестные категории приватности молча отбрасываются | предупреждение |
| `core/evaluate.py:111-116` | подстроковое совпадение завышает recall | точное сравнение basename |
| `core/shortcut.py:34-38` | путь Start Menu собран из `%USERPROFILE%/AppData/...` вместо Known Folder API | `SHGetKnownFolderPath(FOLDERID_Programs)` |
| `core/doctor.py:38-43` | `_dir_size` не ловит `OSError` | try/except |
| `core/app.py:811-845` | утечка instance lock при исключении | try/finally |
| `core/watch.py:206,229` | в event-режиме `state.cycles` инкрементируется каждые 0.2 с → счётчик завышен ~в 5 раз | инкрементировать только при `ready` |
| `core/images.py:26,125` + `config.py:103` | `THUMB_SIZE=320` продублирован; `config.thumb_size` не действует | передавать `config.thumb_size` в `thumbnail_bytes(size=...)` |
| `run.py:56` + `core/app.py:41,785-787` + `hotkeys.py:39-52` | глобальный hotkey `ctrl+alt+space` захардкожен и не берётся из `hotkeys.json` (там `focus: ["/"]`) | читать `read_hotkeys(config.home)` |
| `AutoFind-debug.spec:9` | собран из `rapidocr_onnxruntime` при зависимостях на `rapidocr` | `collect_data_files('rapidocr')` |
| `afdiag.spec:12,14` + `afdiag2.spec:12,14` | абсолютные `C:/Windows/System32/msvcp*.dll` и дубль spec на неверсионированном `_diag.py` | `os.environ["SystemRoot"]`, один parametrised spec |
| `.gitignore:27` | `_diag.py` проигнорирован, но используется в spec | версионировать или убрать spec |
| `core/ocr/languages.py:29-150` | таблица ~70 языков и псевдонимы в коде, хотя модуль называет себя «одной таблицей» | перенести в `ocr-languages.json` |
| `core/ocr/indra.py:64-90,158,316,423,477,758,856-871` | URL с `v1.1.0`, 8 пар «байты + SHA-256`, timeout=120, пороги инференса в исходнике | вынести в `ocr-indra/manifest.json` |
| `core/updater.py:39-51` | репозиторий, URL, TIMEOUT=30/300, лимит 512 MiB, `ping 127.0.0.1` в коде | конфиг/env |
| `core/maintenance.py:52,56,61` | пороги уборки 24ч/1ч/72ч захардкожены | `CleanupConfig` |

---

## 4. ХАРДКОД (что вынести в конфиг/константы)

**Системные и пользовательские пути**
- `core/api.py:2523` — `C:\Users\h1ile\Downloads` **✓** (утечка имени пользователя в репозиторий)
- `core/cli.py:1677` — `C:\Users\you\AppData\Roaming\kensaku` (в справке, приемлемо)
- `core/cli.py:1205` — `Path.home()/"Desktop"`
- `core/shortcut.py:34-38`, `core/watch.py:408`, `afdiag*.spec:12,14`
- `core/connect.py:64-81` — пути конфигов клиентов и относительный `Path(".vscode")` (зависит от CWD)

**Порты и версии**
- `8765` продублирован: `run.py:52`, `cli.py:111,115,276`, `api.py:2409,2541`, `app.py:795,895`, `openapi.py:17` → единая константа `DEFAULT_PORT`
- `openapi.py:17` — мёртвая константа `SERVER_URL`
- `mcp.py:28` — `PROTOCOL_VERSION = "2024-11-05"` без переговоров
- `mcp.py:457` — fallback `"0.1.0"`; четыре разных значения версии (см. §2.5)
- `api.py:585` — `server_version = "AutoFind/0.1"` vs `_APP_VERSION` (1392,1877)

**Бюджеты, лимиты, тайминги (влияют на поведение, но не настраиваются)**
- `core/config.py` — предложить единый модуль констант: `MAX_POST_BYTES` (api.py:89,105), `SEARCH_CACHE_LIMIT=48` / `MAX_BYTES=4MB` (222-223), `CHECKPOINT_INTERVAL_S=300` (2580), `RESULT_LIMIT_*` (50/8/5000/12/200/60), `DEFAULT_PORT`, `TEXT_ENCODINGS`, `EMBEDDING_DIM`, `THUMB_SIZE`, `RAM_BM25`, `GPU_ORDER`
- `core/indexer.py:286` — эвристика пула `4/1000/40`; `302-308` — воркеры `12/6/4` и `cpu_count() or 2`; `363` — батч `64`; `695-697,957` — `1<<20`; `837` — `timeout=2`; `871,892,913` — `100_000/3000/100_000` backfill
- `core/chunker.py:25,71` — `1200/200` продублированы в двух сигнатурах
- `core/scanner.py:195-196,121,133-135,148` — `20*1024*1024`, `32`, sniff `4096`, sample `1024`, порог `0.04`, chunk sha `1<<20`
- `core/ignore.py:21` — `MAX_IGNORE_FILES=2000` (и лимит не глобальный, см. §2.2)
- `core/query.py:459` — допуск ±1 KiB для `size:2mb`; `556,566,578,589,598` — обрезки `64/96/120/200`
- `core/group.py:28-35,76` — `15*60`, `10`, `2`, `0.6`, `640`, `range(3)`
- `core/search.py:93-94,322,325,593,631,682-683` — `SIMILAR_MAX_HAMMING=8`, `SIMILAR_MIN_COSINE=0.99`, `ratio>=0.78`, `examined>=30`, `pool*4`, `len(hits)<3`, `min(pool,25)`
- `core/store.py:60-63,1273,1281-1283,1289` — `MAX_REGEX_*`, `mmap_size=1GB`, `cache_size=-2000/-32000`, `busy_timeout=5000`, `wal_autocheckpoint=0`, `SCHEMA_VERSION=12`, `TEXT_BLOCK_CHARS=16384`, `'AFS1'`, `LIMIT 10/40/4000`, `[:2000]/[:8000]`, `len(members)>60`
- `core/bm25.py:43-46,50-55,484` — `K1=1.2`, `B=0.75`, `MAX_PREFIX_TERMS=200`, `SEG_POSTINGS=1M`, `SEG_CACHE_BYTES=16MB`, zstd level=3
- `core/nameindex.py:44,48,69,254,266` — `HARD_CAP=20000`, `MAX_ENTRIES=1.5M`, `ALPHA_VERSION=2`, `.nameidx`, `[:10]`
- `core/vectors.py:84-85,93,407,420,607-615` — `HOT_MAX_ROWS=50k`, `HOT_MIN_ROWS=10k`, `SKETCH_MIN_ROWS=100k`, `max(4096,40*limit)`, `4*sigma`, backoff `0.05*(attempt+1)`
- `core/extract/*` — `delimited.py:21-31` (8K/20/200/8K, разделители); `archive.py:18-20` (400/2MB/8MB); `email.py:18-19` (200/500K); `pdf.py:36,41,51-55,107` (50 chars, 50M px, 10/20/6 pages, zoom 3.0); `dates.py:30,35` (4000/1990); `office.py:34-60` (20K/500/400/64MB/256MB/2M/20K/8MB)
- `core/embed/*` — `hashing.py:17-24` (384/4/0.35); `onnx_e5.py:17-39` (512/32/384/префиксы); `static.py:28,32-33,42,48` (512, `<8sII`, `SFASTTBL`, 384); `visual.py:21-55,139-142` (228/512/77/224×224/mean/std)
- `core/ocr/*` — `__init__.py:66,73` (4/8); `engine.py:158,416,419,448-454,580` (limit=4, psm 3, timeout=120, chi_sim+jpn+kor+eng); `script.py:37-41` (8.0/2.0/1.6); `indra.py:158,316-317,423-425,477,758-760,856-871`
- `core/watch.py:204,2580` — `max(interval*15, 60.0)`, `time.sleep(300)`
- `core/maintenance.py:52,56,61` — `24*3600`, `60*60`, `3*24*3600`
- `core/updater.py:39-51` — TIMEOUT 30/300, 512 MiB, SUMS_NAME, REPLAY_DELAY=2
- `core/cli.py:34,668,1713,621,890,1225,1696,1828` — ширина бара 28, таймаут 3, `position%100`, `[:3]/[:2]/[:1]/[:5]/[:160]`

**Рекомендация:** завести `core/constants.py` (или расширить `core/config.py`) с именованными константами и env-оверрайдами, затем заменить магические числа по списку выше. Для форматов (BM25, nameidx, static pack, OCR-пак) версию формата хранить рядом с MAGIC/HEADER.

---

## 5. ЧТО НЕ ЯВЛЯЕТСЯ ПРОБЛЕМОЙ (проверено)

- `compileall` проходит без ошибок; синтаксис во всех 68 core-файлах корректен.
- Голых `except:` и `assert` в продакшен-коде нет. 90 случаев `except → pass` есть, но большинство — осознанный best-effort (закрытие файлов, cleanup); системная проблема лишь в том, что они не логируются.
- Все 13 skip-тестов условны (нет OCR-движка, нет Node, нет DPAPI, не win32) — это не отключённые тесты, а корректная адаптация к окружению. `assert True` / `if False` в тестах не найдены.
- `CJK_RE` в `search.py` не содержит capturing groups — `re.split` в строке 193 безопасен.
- `/api/audit` реализован (пометка в PROJECT_MAP устарела).

---

## 6. ИЗ ПРЕЖНИХ ОТЧЁТОВ — ВСЁ ЕЩЁ ОТКРЫТО (сверено с кодом)

| Где | Проблема | Замена | Источник |
|---|---|---|---|
| `core/vault.py:234` `✓` | passphrase-режим (AFVK2, Argon2id/scrypt) написан, но **ни CLI, ни UI не вызывают** `enable_passphrase` — единственные вызовы в `tests/test_vault.py:75+`. Пользователь не может включить портативный vault | добавить `autofind vault passphrase set` и кнопку в Encryption-панель; дополнить i18n | NIGHT_REPORT.md:76, ROADMAP.md:11 |
| `core/updater.py:39` + `tests/test_updater.py:130-174` | «test actual download/replace flow on a real release» не выполнен: тесты на `example.invalid` и `mock.patch(urlopen)` | integration-прогон против настоящего latest release с проверкой SHA256SUMS и staged-файла | NIGHT_REPORT.md:78 |
| `core/store.py:2881-2886,2602-2627` | `usage_map()` материализует всю таблицу usage; `_FILEVEC_CACHE` держит весь packed-bits массив без byte-лимита. BENCH: USS 565 MiB на 1M против 341 у kensaku | точечные SQL-запросы вместо полного `usage_map()`; byte-cap с вытеснением через `memguard.SoftLimit` | BENCH.md:49-56, ROADMAP.md:8 |
| `core/api.py:2689`, `config.py:80`, `watch.py:118` | 93 строгих `except …: pass` и ~207 широких «глотаний» по core/ — не тронуто | подключить `LOG.write` к каждому блоку, затем заменить на типизированные исключения; начать с api.py:2689, watch.py:118 | FIX_REPORT.md:52-53 |
| `core/config.py:23` `✓` | `os.environ.setdefault("AUTOFIND_EMBEDDER","hashing")` меняет глобальное окружение при импорте | убрать запись; читать в `get_embedder()`/CLI с явным дефолтом `IndexConfig` | FIX_REPORT.md:57-58 |
| `core/store.py` (3520 строк), `core/api.py` (2689) | монолиты выросли; `config.py:123-126` всё ещё объявляет 3 vector_backend’а, хотя `_setup_vectors` всегда `filebin` | разнести store на storage/sidecars/vectors, api по группам маршрутов; pack-логику в отдельный legacy-модуль | FIX_REPORT.md:54-55, OPTIMIZATION_ROADMAP.md:118-121 |
| `core/api.py:2622-2625`, `vectors.py:1171` | pack-слой жив (1171 строка), мёртвая ветка в `_prewarm` | удалить ветку, переименовать настройку в `image_vector_backend`, пометить модуль unreachable since v9 | OPTIMIZATION_ROADMAP.md:48,118-121 |
| `core/importers.py:1` | EFU/Listary-импорт реализован, но в CLI нет команды; единственный импорт — в тесте | `autofind import <efu\|listary> <path>` → `detect_format()` → `read_list()` → `seed_index()` | OPTIMIZATION_ROADMAP.md:49,124; NIGHT_REPORT.md:84 |
| `core/extract/__init__.py:28-40`, `core/logger.py:21` | нет parquet, нет crashlog-канала, нет загрузчика внешних экстракторов | добавить `parquet` с опциональным `pyarrow`, crashlog в logger, opt-in plugin loader | NIGHT_REPORT.md:100 |
| `core/api.py:2111-2118` | `_static()` отдаёт `target.read_bytes()`; `app.js` (3199 строк) и `i18n.js` (1671) не минифицируются | build-шаг `*.min.js` и отдача их в `_static()` (или gzip) | OPTIMIZATION_ROADMAP.md:51,126 |
| `core/models.py:91-92`, `core/ocr/indra.py:109-110` | два почти идентичных загрузчика: `file_sha256`, `download_pack`, `.part`-staging, hf/hf-mirror, проверка SHA | общий `core/download.py` с artifact spec (url, bytes, sha256) | OPTIMIZATION_ROADMAP.md:52,127 |
| `core/nameindex.py:44,648-652` | `HARD_CAP=20_000` не настраивается (осознанная уступка ради латентности) | `IndexConfig`/env `AUTOFIND_NAME_HARD_CAP` с дефолтом 20000 | docs/decisions.md:261-262 |
| `core/vectors.py:436-439,587-603` | двухфазный 1-bit sketch на реальном корпусе не срабатывает (порог > косинуса), но массивы всё ещё пишутся в пак | убрать запись по умолчанию, оставить opt-in для кластеризованных данных | docs/decisions.md:783-797 |

**Устаревшие пометки в `PROJECT_MAP.md` (снять):**
- `:202` — `/api/audit` реализован целиком (api.py:754-755,1052-1111, включая `files`-view и `store_sealed`).
- `:49` — делегация CLI→сервер есть (cli.py:636-676, `--local` в 107).

**Проверено и НЕ является открытой проблемой:** `.msg`, zh-CN, CLIP, MCP-пагинация, OpenAPI, CLI export, self-test, RAM-limit, idle-гейт, сетевые диски — в коде присутствуют. Мусор в корне (`_diag.py`, `afdiag*.spec`, `build/`, `dist/`) в `.gitignore:5-6,27-29` и не отслеживается git.

---

## 7. ТЕСТЫ (51 модуль `test_*.py`, 599 тестовых функций)

**Скипнутых потенциально 25:** 9 явных `skipTest()` + 16 под class-декораторами (`test_afnative.py:23` — 10, `test_core.py:161` — 2, `test_static_embedder.py:71` — 4). Все условия — про окружение (нет движка/Node/DPAPI/нативной сборки), не «отключённые» тесты. Пустых тестов и `assert True` нет.

| Где | Проблема | Замена |
|---|---|---|
| `.github/workflows/tests.yml:47` + `pyproject.toml:25` `✓` | CI ставит `.[dev,pdf,docx,onnx,watch]`, где `pdf = ["pypdf>=4.0"]`; `pymupdf` есть только в `requirements.txt`, а `test_pdf_scanned.py` (43-52,98,116,137,141) делает `import pymupdf` → `ModuleNotFoundError` | добавить `pymupdf>=1.24` в `pdf` extra или `pip install -r requirements.txt` в workflow |
| `tests/test_models.py:86-88` `✓` | `self.addCleanup(mock.patch.dict(...).start)` — патч **никогда не применяется во время теста**; `DownloadTests` пишет в реальный `~/.autofind/models`, а cleanup только устанавливает переменную в удалённый tmp | `p = mock.patch.dict("os.environ", {...}); p.start(); self.addCleanup(p.stop)` |
| `tests/test_final_wave.py:110` `✓` | `_os.environ["AUTOFIND_HOME"] = ...` без восстановления → все последующие тесты читают home из удалённого `TemporaryDirectory`, порядок-зависимые падения | `with mock.patch.dict(os.environ, {"AUTOFIND_HOME": str(...)}):` |
| `tests/test_ocr_indra.py:174-175` | `if __name__ == "__main__": unittest.main()` **в середине файла** — до `VocabTests`, `RtlTests`, `OrderTests`, `LeaderTests`, `PackTests`, `EngineTests`; при прямом запуске ~20 тестов молча не выполняются | перенести блок в конец (дубль уже есть на 486-487) |
| `tests/test_batch_d.py:90` | `... if False else None` — отключённая проверка i18n; реально трижды читается `i18n.js` и проверяются две подстроки, цикл `for lang` проверяет одно и то же | убрать `if False`, `json.loads(...)`, проверять ключи по языкам |
| `tests/test_batch_d.py:50-53` | тест `test_health_payload_has_version_and_uptime` проверяет только `assertTrue(_APP_VERSION)`, не обращаясь к `/api/health` | поднять сервер и проверить `payload["version"]`, `payload["uptime_s"]` |
| `tests/test_afnative.py:23` | весь класс висит на `@unittest.skipUnless(_HAS_NATIVE)`; workflow не выполняет `native/build_native.py`, в репо только `.pyd` под CPython 3.13 → в матрице 3.11/3.12/3.13 все 10 тестов пропускаются | добавить шаг сборки в workflow или явно пометить как non-CI |
| `tests/test_core.py:161,183,186` | `OcrTests` целиком под class-skip; русский тест исчезает вместе с движком; японский дополнительно пропускается из-за жёсткого `C:/Windows/Fonts/YuGothR.ttc` | шрифт из фикстуры; русский тест вынести из class-skip |
| `tests/test_core.py:75`, `tests/test_pdf_scanned.py:34` | `ImageFont.truetype("arial.ttf",34)` — на Linux fallback на мелкий bitmap, меняя размер текста в ~10 тестах (OCR + визуальные сравнения) | положить TTF в `tests/fixtures/` |
| `tests/test_pdf_scanned.py:67` | `Path("C:/Windows/Fonts/arial.ttf").exists()` — на не-Windows тест всегда skip | centralized fixture / `skipUnless(sys.platform=="win32")` |
| `tests/test_languages.py:114-116` | `READY["ready"] = True/False` без `finally` и без восстановления → при падении глобал остаётся `True` | сохранить старое значение и вернуть в `finally` |
| `tests/test_languages.py:90-99` | сервер на фиксированном порту 8791 для каждого из 3 тестов, `tearDown` не делает `join()` | `port=0`, брать `server.server_address[1]`, `self.thread.join(timeout=5)` |
| `tests/test_openapi.py:34`, `test_background.py:52`, `test_languages.py:91`, `test_mcp_http.py:34,48`, `test_app.py:97`, `test_launcher.py:88` | захардкоженные порты 8792/8790/8791/8765/8899/8123/9123 — конфликт / TIME_WAIT ломает набор | `port=0` + фактический порт |
| `tests/test_dedup_search.py:186` | `assertLess(dedup_ms, 12.0*(1-0.28)+6.0)` — привязка к стенному времени (~14.64 мс) в CI | убрать timed-assert, замер вынести в `bench_names.py` |
| `tests/test_hardening.py:47,111` | оба теста (`test_xss.js`, `test_pin_ui.js`) пропускаются без Node; `.js`-файлы не собираются `unittest discover` → проверки XSS/пин-UI исчезают полностью | `actions/setup-node` в CI или переписать на Python |
| `tests/test_vault.py:171,208,200,211,219` | `vault_mod.enable()` выбрасывает `VaultError` на не-Windows, но `SealMigrationTests` без skip → падения на Linux; `install(None)` меняет глобал без восстановления | `enable_passphrase(...)` или `@unittest.skipUnless(os.name=="nt",...)`; вернуть `_CURRENT` |
| `tests/test_updater.py:223-224` | `subprocess.run("cmd /d /c " + cmd)` и `subprocess.CREATE_NO_WINDOW` — нет на Linux, модуль без platform skip | `@unittest.skipUnless(os.name=="nt",...)` и `getattr(subprocess,"CREATE_NO_WINDOW",0)` |
| `tests/test_two_layer.py:253-255` | `index.db.v3.hot` захардкожен — тест ломается при смене `PACK_VERSION` | `f"index.db.v{vecstore.PACK_VERSION+1}.hot"` |
| `tests/test_scale.py:25` | `abs(hash((path,name))) % 100000` — соль `PYTHONHASHSEED`, `file_id` недетерминирован | `zlib.crc32(...) % 100000` |
| `tests/test_sketch_search.py:53` | голый `assert pack is not None` в хелпере исчезает при `python -O` | `raise AssertionError` |
| `tests/test_finders.py:122-123` | walrus-присваивания с неиспользуемыми значениями, два лишних экземпляра `HashingEmbedder()` | один `embedder = HashingEmbedder()` |
| `tests/test_pack_version.py:118`, `test_two_layer.py:98` | инвертированные сообщения assert (говорят противоположное проверяемому) | поправить тексты |
| `tests/bench_names.py:31` | синтетические `C:\Users\someone\...` — бенчмарк не кроссплатформен | `str(Path.home()/...)` |
| `tests/test_search_features.py:118` | `r"C:\\repo\\readme_00001234.log"` — двойные слеши в raw-строке, тогда как остальные тесты файла используют одинарные | `"C:\\repo\\..."` |
| `tests/test_parallel_index.py:38` | `get_embedder("auto", ...)` — выбор зависит от скачанной модели, тест может сравнивать разные типы эмбеддеров | явно `"hashing"` |
| `README.md:766`, `PROJECT_MAP.md:94` | заявлено «312 tests» / 48 файлов; реально 599 функций в 51 модуле. `tests/queries/queries.json` (20 запросов) и `labels_demo.json` ни один тест не открывает — acceptance set не проверяется | обновить цифры; добавить тест, читающий queries.json |

---

## 8. ФРОНТЕНД (`web/`, 4 файла / 7 880 строк)

**Проверено отдельно:** все 39 уникальных путей `/api/...`, вызываемых в `app.js`, существуют в `core/api.py` — несовпадений нет. `esc()` применяется в `highlight()` (111-129) и во всех полях карточек; эксплуатируемого XSS не найдено. Не экранируются только `src` у `hit.thumb`/`item.thumb`/`data.image.thumb` (754, 999, 1767) — сейчас это серверные `/api/thumb?id=N`, но для устойчивости стоит `esc()`. `console.log`, `TODO`/`FIXME` и дубликаты ключей i18n не найдены, `checkTranslations()` возвращает `[]`.

| Где | Проблема | Замена |
|---|---|---|
| `app.js:2922,2979,3037` `✓` | на `[data-annotate-run]` висят **три** слушателя (на `#annotate-body`, на `#modal-grid`, на `document.body`), событие не останавливается → тройной POST `/api/tag` и тройной toast | оставить один document-level обработчик; убрать 2922 и 2979 (или `event.stopPropagation()`) |
| `app.js:1504,1516,2880` | у `.dup-pick` нет `change`-обработчика; `updateActionButtons()` вызывается только из `setPanel/setDups` и кнопок → после ручного выбора `#dups-delete` остаётся disabled | `$("dups-body").addEventListener("change", e => { if (e.target.matches(".dup-pick")) updateActionButtons(); })` |
| `app.js:919-922` | `chip.dataset.base` кэширует текст при первом `paintFacets()`; `applyStaticText()` (i18n.js:1638-1640) меняет `textContent`, но не `dataset.base` → чип не меняет язык | не кэшировать, брать из `t("kind." + (chip.dataset.kind \|\| "all"))` |
| `app.js:2622` + `i18n.js:1638-1640` | `#sort-value` имеет `data-i18n="sort.best"` → смена языка сбрасывает «newest» в «best match» | после `setLang`: `$("sort-value").textContent = t("sort." + state.sort)` |
| `app.js:1543` | `openConfirm(t("confirm.deleteTitle"), ...)` подставляет заголовок в тело; ключ `confirm.deleteText` с `{n}` не используется → «Are you sure?» дважды | `openConfirm(t("confirm.deleteText", { n: ids.length }), ...)` |
| `app.js:2772-2785` | Backspace не очищает hotkey (`chordOfEvent` → `"backspace"`); `input.dataset.clean` читается, но нигде не выставляется → `saveHotkeys()` на каждом blur | в keydown: `if (event.key === "Backspace") { preventDefault(); input.value=""; return; }`; ставить `data-clean="1"` в `renderHotkeys()` |
| `app.js:1290-1335,2728-2741` | `renderSeal()`/`pollSeal()` только после клика по `#seal-btn`; при открытии Encryption всё «—», `state.seal===null` → action всегда `"on"` | в `setDrawerTab("encryption")` вызывать `pollSeal()` |
| `app.js:1268-1272` | `refreshPrivacy()` только из purge-обработчика (2721); при старте/открытии чекбокс выключен, счётчики «—», список пуст | вызывать в `setDrawerTab("privacy")` и в init |
| `app.js:1116-1118` + `index.html:528-529` | `/api/status` отдаёт `default_roots` (api.py:1391), но `loadStats()` игнорирует → панель Index с пустым полем путей | `if (!$("roots").value) $("roots").value = (status.default_roots\|\|[]).join("\n")` |
| `app.js:2631-2654` + `index.html:352-353,433-434,459-460` | `.filter-menu` для `f-type`, `f-period`, `f-collection` пусты и нигде не заполняются — пикеры открывают пустой список | в init копировать `<option>` из скрытых `<select>` в `.filter-menu` как `<button data-value>` |
| `app.js:712` | `state.chips` присваивается и не читается; `data.hints` и `data.images` из ответа игнорируются → мертвы ~50 ключей `chip.*`, `hint.*`, `typeVal.*`, `weekday.*` и функция `clearChip` (740) | рендерить чипы/хинты/визуальные результаты в `renderResults()` |
| `index.html:947-950` | overlay обещает `Ctrl+Alt+D`, `Ctrl+Alt+=`/`-`, `Ctrl+Shift+C` — обработчиков в app.js нет | реализовать в глобальном keydown (3121-3150) или убрать строки |
| `index.html:913-914` + `style.css:1838-1851` | `#audit-list`/`#audit-warn` есть в разметке и CSS, но `loadAbout()` (2035-2064) заполняет только `#about`; вкладка «Your data» дублирует About, серверные `sizes`/`encrypted` игнорируются | строить `.audit-row` из `audit.sizes` и `audit.files_on_disk` |
| `i18n.js:1553-1556,1569-1582` `✓` | перевод `zh` полный (481 ключ, строка 1105), кнопки `data-lang="zh"` есть в HTML, но `LANGUAGES` содержит только `en/ru`, `detectLang()` возвращает `en` для китайской системы | добавить `{code:"zh",label:"中文"}`, убрать ограничение в `detectLang()` |
| `i18n.js:453-454,977-978,1458-1459` | `why.preview` — точная копия `why.listed` во всех трёх языках (незаполненный плейсхолдер) | отдельный текст, напр. «Listed by image similarity» |
| `app.js:84-87,740-747,1844` | мёртвый код: `KIND_LABEL`, `clearChip()`, `SIZE_UNITS` не вызываются | удалить или подключить `clearChip()` к рендеру чипов |
| `app.js:982-995,778-794` | `recentImages` кэшируется навсегда, `TEXT_CACHE` ключуется только по `file_id` → после переиндексации плитка показывает старый текст | сбрасывать кэш в `deleteIds`/после `pollJob`; ключ `${id}:${mtime}` |
| `app.js:1768`, `style.css:947-973,439-445` | `data-pv-id` пишется, но не читается; CSS `.kind-tile .mail`, `.suggest .s-head` не создаются рендером | удалить мёртвое или переключить email на `.mail` |
| `index.html:938` + `app.js:2831-2850` | текст обещает зум колесом мыши, но `wheel`-обработчика нет | `stage.addEventListener("wheel", ..., {passive:false})` |
| `app.js:40,672,1144,1232,1332,1609,2103-2105,2124,2163,2236,2264,2482,2484,2546` | тайминги литералами: toast 3200, debounce 150, pollJob 450, watch 6000, seal 400, boot 900/120/70, приветствие 700/110/900, onboarding 5000, анимации 550/620/460/300/320/160 | `const TIMING = Object.freeze({...})` |
| `app.js:676,792,989,809,664,1961,1998,3110,556` | лимиты вшиты: `limit:"12"`, `chars=4000/1600/0`, `/api/images?limit=6`, `slice(0,6/8/7/24)`, `limit:"5000"`, `WALL_MAX_MB=12` | `const LIMITS = {pageSize:12, previewChars:1600, ...}` |
| `app.js:307-316,2815-2821` + `index.html:685-748` | `DEFAULT_LOOK` и диапазоны `clamp()` дублируются в атрибутах слайдеров в трёх местах | генерировать атрибуты из одного `DEFAULT_LOOK`/`LOOK_RANGES` |
| `app.js:278` + `index.html:16` | ключ `localStorage` `"autofind.wall"` продублирован | единая константа |
| `app.js:762-767`, `style.css:411-412,1100,1105-1106` | цвета акцентов в JS (`#c94747/#3f6fc4/#8a63b8`) и CSS (`#f2c14e`) не поддаются темизации | CSS-переменные `--sheet-doc`, `--sheet-pdf`, `--sheet-mail`, `--star` |
| `app.js:281-287,2107-2118` | пресеты обоев и 10 приветствий захардкожены, дублируя i18n/LANGUAGES | вынести в конфиг |
| `app.js:828,809` | `Math.min(order,12)*38`, 12 строк, 60 символов | named constants |

---

## 9. ИТОГОВЫЙ ПРИОРИТЕТ РЕМОНТА

1. **Безопасность/корректность обновлений:** `cli.py:393-394` (SHA256), `updater.py:293` (`sys.frozen`), `vault.py:165` (AFS1), `config.py:23` (env side-effect).
2. **Потеря данных / падения индексации:** `indexer.py:372`, `delimited.py:133`, `pdf.py:184`, `docx.py:36-41`, `nameindex.py:169-180,760-772,806-817`, `vectors.py:399-406`, `store.py:101-103`.
3. **Молча неработающие функции:** `-type:`/`-re:` (`query.py`), `collection:` union (`search.py:511`), `--vector-backend` (`store.py:1315`), passphrase-vault (`vault.py:234`), EFU/Listary CLI (`importers.py`), zh locale (`i18n.js:1553`), 3 фильтр-меню и audit-вкладка в UI.
4. **Гонки:** OCR `_patience`, `store._BM25` (2088), `vectors` sketch-счётчики, `api.SEARCH_CACHE`, `embed._EMBEDDER_CACHE`, rapidocr `_engine`, MCP single store.
5. **Тесты/CI:** pymupdf в extras, `test_models.py:86`, `test_final_wave.py:110`, native build в workflow, node для XSS-тестов.
6. **Хардкод:** единый `core/constants.py` + конфиг форматов/моделей/OCR/таймингов UI; убрать `C:\Users\h1ile\Downloads` из `api.py:2523`.

---

*Отчёт подготовлен поenylому проекту: карта → все файлы → 8 параллельных аудитов → ручная перепроверка критичных позиций. Ни один файл исходников не изменялся; создан только этот отчёт.*




