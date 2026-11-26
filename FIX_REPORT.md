# FIX_REPORT.md — исправления после аудита

Дата: 2026-11-25. Все правки внесены точечно; существующие незакоммиченные
изменения пользователя не перезаписаны.

## Что исправлено

| # | Где | Проблема | Почему это важно | Что сделано |
|---|-----|----------|------------------|-------------|
| 1 | `web/app.js` — `highlight()` | Сниппет из проиндексированного файла вставлялся в `innerHTML` без экранирования | Stored XSS: файл с `<img src=x onerror=...>` выполнял JS при показе результата | Экранирование перенесено в саму функцию `esc(text)` до подсветки; комментарий обновлён |
| 2 | `core/api.py` — `_send()` | Ответы отдавались без CSP и nosniff | Даже при экранировании любая новая дыра в DOM давала выполнение скрипта | Добавлены `Content-Security-Policy` (с хэшем единственного инлайн-скрипта), `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` |
| 3 | `core/api.py` — `IndexJob._run()` | `SEARCH_CACHE` чистился только при записи | После переиндексации тот же запрос показывал старые результаты | `clear_search_cache()` в `finally` индекс-задачи |
| 4 | `core/api.py` — `do_POST()` | Ошибки любого POST-обработчика уходили наружу; `int(Content-Length)` падал на мусоре; тело не ограничивалось | Клиент получал сброс соединения вместо JSON-ошибки; бесконечное тело | Добавлен wrapper `do_POST()` с `try/except`, guarded разбор `Content-Length`, лимит `MAX_POST_BYTES = 32 МБ`, флаг `_replied` против двойного ответа на keep-alive. Аналогично защищён `do_GET` |
| 5 | `core/watch.py` — `EventFan` / `Watcher.run()` | `ReadDirectoryChangesW` блокировал поток; `stop()` выставлял Event, но разбудить поток не мог | Утечка по потоку и хендлу на каждый root при выключении/перезапуске вотчера | Хендлы registry + `CancelIoEx` в `EventFan.stop()`, join потоков, обработка `ERROR_OPERATION_ABORTED (995)`, `fan.stop()` в `finally` у `Watcher.run()` |
| 6 | `core/store.py` — `regex_chunks()` | Пользовательский `re:` без таймаута и лимитов гонялся по всему тексту индекса | `(a+)+$` на 20 МБ вешал сервер | Отсечение паттернов >256 символов, вложенных квантификаторов и `{>4096}`; лимит сканируемого текста 64 МБ на запрос (`_regex_is_unsafe`) |
| 7 | `core/api.py` — `_export()` | Имена файлов попадали в CSV как есть | Формульная инъекция в Excel (`=HYPERLINK(...)`, DDE) | Новый `_csv_cell()` с префиксом `'` для `= + - @ tab CR` |
| 8 | `core/store.py` — `save_search_query()` | Обрезка по `at` (секунды) и лишний `commit()` | При совпадении времени удалялись все строки с этим timestamp; двойной коммит двигал WAL | Обрезка по `rowid` с tie-breaker; коммит оставлен вызывающему `Handler._write()` |
| 9 | `core/search.py` / `core/api.py` | Выбор fuzzy-бэкенда зависел от `"numpy" in sys.modules` | Один и тот же запрос ранжировался по-разному в зависимости от порядка импортов | Явный параметр `fuzzy_fast`, сервер передаёт `fuzzy_fast=True`; CLI сохраняет ленивый режим |
| 10 | `core/api.py` — `_host_ok()` | `Host.split(":")[0]` превращал `[::1]:port` в `[` | IPv6-loopback отклонялся, хотя был в allow-list | Разбор через `urlparse("//" + Host).hostname` |
| 11 | `core/updater.py` — `check_update()` | `"win" in name` совпадало с `darwin` | macOS-архив мог быть предложен как Windows-обновление | Явная проверка `windows/win32/win64/^win[-_.]`, исключение `darwin/macos/linux` |
| 12 | `core/indexer.py` | `zip(jobs, prepared_batch)` молча терял файлы при коротком ответе воркера | Прогресс считал файл обработанным, хотя он не попал в индекс | Проверка длин, учёт в `stats.failed`/`stats.errors`, явный `strict=False` |
| 13 | `core/watch.py`, `core/search.py` | Мелкие линтовые шероховатости (f-строка без плейсхолдера, неиспользуемый импорт) | Шум в отчётах линтера | Убраны; `subsequence_score` оставлен как ре-экспорт (его импортируют тесты) |

## Проверка

- `python -m unittest discover -s tests -t .` → **590 тестов, OK** (было 583).
- `python -m compileall -q core web tools run.py` → без ошибок.
- `node --check web/app.js`, `node --check web/i18n.js` → без ошибок.
- `ruff check <изменённые файлы> --select E9,F` → **All checks passed!**
- Хэш инлайн-скрипта в CSP пересчитан из актуального `web/index.html`.

Добавлен `tests/test_hardening.py` (7 тестов): ReDoS-фильтр, CSV-инъекция,
IPv6 Host, обрезка `recent_searches` при одинаковом времени, явный выбор
fuzzy-бэкенда, чужие Host/Origin.

## Дополнительно после анализа отчёта

- `core/api.py` — при `invalid content length` / `payload too large` соединение теперь
  закрывается (`close_connection = True`): невычитанное тело больше не портит
  следующий запрос на keep-alive.
- `core/store.py` — ReDoS-фильтр усилен: лимиты на число групп/квантификаторов и
  отсечение смежных квантованных групп (`(a+)(b+)+`), помимо вложенных.
- `core/indexer.py` — короткий ответ воркера теперь не только считается в
  `stats.failed`, но и перевыполняется в процессе, чтобы файлы не терялись.
- `tests/test_xss.js` + `tests/test_hardening.py` — DOM-тест `highlight()` без
  jsdom: индексированный `<img onerror=...>` остаётся текстом.

Проверка после правок: `pytest tests -x -q` → **591 passed, 169 subtests**.

## Что осталось (не исправлял намеренно)

- ~180 подавленных исключений `except Exception: pass` — массовая замена меняет
  поведение и требует отдельного прохода с логами.
- Монолит `core/store.py` (3125 строк) и `core/api.py` (2355 строк), три
  бэкенда векторов — архитектурный долг, не баг; требует отдельного плана.
- Отсутствует конфигурация ruff/mypy в `pyproject.toml` и JS-тесты.
- `config.py` выставляет `AUTOFIND_EMBEDDER` через `os.environ` при импорте:
  поведенческое изменение, трогать осторожно.

## Скиллы

Установлены в `C:\Users\h1ile\.claude\skills\`: devops-build-optimizer,
code-refactoring-expert, architecture-optimizer, performance-optimizer,
frontend-optimizer, database-optimizer, clean-simple-code.
