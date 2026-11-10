/* Interface strings. English is the source language; Russian is the translation.

   Two rules keep this from rotting:

   1. Every key exists in every language. `checkTranslations()` runs in the
      console (and in the tests) and names any key that is missing, so a new
      string cannot ship in one language only.
   2. Nothing user-visible is written inline in app.js. If it is text a person
      reads, it comes from here — including the plural forms, which are a
      language feature, not a formatting detail: English has two, Russian three.

   The search engine does not put words in here. A chip arrives from the server
   as a kind plus a value ("time" + "past week") and the words are chosen below,
   which is the only way the interface can be translated without translating the
   query compiler.
*/

const I18N = {
  en: {
    "app.title": "AutoFind — local search over files and screenshots",
    "win.hide": "Hide the window (Ctrl+Alt+Space brings it back)",
    "win.quit": "Quit AutoFind",
    "suggest.recent": "recent",
    "suggest.saved": "saved",

    "boot.starting": "starting",
    "boot.index": "opening the index",
    "boot.warm": "warming the vectors",
    "boot.wait": "waiting for the server",
    "boot.ready": "ready · {ms} ms",
    "boot.skip": "click or press Esc to skip",

    "search.placeholder": "search",
    "search.aria": "Search files and screenshots",
    "search.clear": "Clear",
    "search.failed": "Search failed: {error}",
    "search.meta": "{shown} of {sign}{total} · {ms} ms",

    "kind.all": "All",
    "kind.docs": "Documents",
    "kind.code": "Code",
    "kind.pdf": "PDF",
    "kind.docx": "DOCX",
    "kind.image": "Screenshots",

    "tools.folder": "folder",
    "tools.any": "any",
    "tools.since": "since",
    "tools.sort": "sort",
    "tools.status": "Index state",
    "sort.best": "best match",
    "sort.newest": "newest",
    "sort.oldest": "oldest",
    "sort.largest": "largest",
    "sort.name": "by name",

    "cal.open": "Pick a date",
    "cal.prev": "Previous month",
    "cal.next": "Next month",
    "cal.clear": "clear",
    "cal.today": "today",
    "cal.docDate": "match the date printed in the document, not the file date",
    "cal.monthYear": "{month} {year}",
    "cal.months": "January,February,March,April,May,June,July,August,September,October,November,December",
    "cal.weekdays": "Mo,Tu,We,Th,Fr,Sa,Su",

    "status.checking": "checking the index…",
    "status.files.one": "{n} file in the index",
    "status.files.few": "{n} files in the index",
    "status.files.many": "{n} files in the index",
    "status.empty": "the index is empty",
    "status.down": "the server is not responding",

    "idle.title": "Search by meaning, not by file name",
    "idle.body": "One box finds the contents of documents, the text in screenshots and fragments of code. Everything is computed on this computer; nothing leaves it.",
    "idle.recent": "recent screenshots",
    "idle.tip": "tip: sym:parse_query finds the definition, not the file",
    "idle.pinned": "pinned",
    "idle.saved": "saved searches",
    "idle.pinsEmpty": "nothing pinned yet — press the star on any result",
    "idle.savedEmpty": "no saved searches yet — type a query and press the bookmark",

    "card.open": "open",
    "card.folder": "folder",
    "card.preview": "preview",
    "card.similar": "similar",
    "card.showAll": "show all {n}",
    "card.pin": "pin this file",
    "card.unpin": "unpin this file",
    "card.symbol": "definition",

    "pin.added": "Pinned",
    "pin.removed": "Unpinned",

    "saved.save": "save this search",
    "saved.done": "Saved: {query}",
    "saved.failed": "Could not save: {error}",
    "saved.forget": "forget",
    "saved.forgotten": "Forgotten",

    "group.snapshot.one": "snapshot",
    "group.snapshot.few": "snapshots",
    "group.snapshot.many": "snapshots",
    "group.copy.one": "copy",
    "group.copy.few": "copies",
    "group.copy.many": "copies",
    "group.version.one": "version",
    "group.version.few": "versions",
    "group.version.many": "versions",
    "group.failed": "could not load",

    "results.more": "show more",
    "results.similarImages": "similar images",
    "results.nothing": "Nothing found",
    "results.nothingHint": "Try other words, drop the type filter, or index a folder from the panel.",

    "chip.time": "modified: {value}",
    "chip.yesterday": "modified: yesterday",
    "chip.today": "modified: today",
    "chip.dayBefore": "modified: the day before yesterday",
    "chip.week": "modified: in the past week",
    "chip.month": "modified: in the past month",
    "chip.year": "modified: in the past year",
    "chip.weekday": "modified: {value}",
    "chip.daysAgo": "modified: {value} days ago",
    "chip.lastDays": "modified: last {value} days",
    "chip.lastWeeks": "modified: last {value} weeks",
    "chip.lastMonths": "modified: last {value} months",
    "chip.size": "size: {value}",
    "chip.sizeMax": "size under {value}",
    "chip.sizeMin": "size over {value}",
    "chip.sizeLarge": "large files",
    "chip.sizeSmall": "small files",
    "chip.type": "type: {value}",
    "chip.ext": "extension: .{value}",
    "chip.lang": "language: {value}",
    "chip.sort": "sort: {value}",
    "chip.place": "in folder: {value}",
    "chip.symbol": "definition: {value}",
    "chip.exclude": "without “{value}”",
    "chip.filter": "regex {value}",
    "chip.remove": "remove “{source}”",

    "typeVal.image": "image",
    "typeVal.doc": "document",
    "typeVal.sheet": "spreadsheet",
    "typeVal.presentation": "presentation",
    "typeVal.pdf": "pdf",

    "weekday.monday": "last Monday",
    "weekday.tuesday": "last Tuesday",
    "weekday.wednesday": "last Wednesday",
    "weekday.thursday": "last Thursday",
    "weekday.friday": "last Friday",
    "weekday.saturday": "last Saturday",
    "weekday.sunday": "last Sunday",

    "hint.unknown_lang": "unknown language “{value}”",
    "hint.folders_not_indexed": "folders are not indexed — search for files",
    "hint.bad_modified": "did not understand “modified:{value}” — use <7d, >30d or 7d..30d",
    "hint.bad_size": "did not understand “size:{value}” — use >2mb, <100kb or 1mb..10mb",
    "hint.did_you_mean": 'did you mean “{value}”? — press to search',
    "ctx.open": "Open",
    "ctx.reveal": "Show in folder",
    "ctx.copy": "Copy path",
    "ctx.copied": "Path copied",
    "ctx.copyFailed": "Could not copy the path",

    "preview.title": "Preview",
    "preview.close": "close",
    "preview.opened": "opened {n} times",
    "preview.never": "never opened",
    "preview.missing": "the file is not on disk",
    "preview.noText": "no text was extracted",
    "preview.failed": "Could not open the preview: {error}",

    "panel.toggle": "Index panel",
    "panel.index": "Index",
    "panel.files": "files",
    "panel.chunks": "chunks",
    "panel.screenshots": "screenshots",
    "panel.withOcr": "with OCR",
    "panel.size": "size",
    "panel.definitions": "definitions",
    "panel.indexing": "Index folders",
    "panel.rootsPlaceholder": "one folder per line",
    "panel.prune": "remove missing",
    "panel.start": "Index",
    "panel.watch": "Watch",
    "panel.ready": "ready",
    "panel.watchingOff": "watching is off",
    "panel.watchOn": "watching: {added} updated · {removed} removed · {batch} in the last batch",
    "panel.watchError": "watching stopped: {error}",
    "panel.duplicates": "Duplicate screenshots",
    "panel.duplicatesNote": "Exact copies only: same file name, same size and same bytes. Select the extras here and delete them.",
    "panel.findDuplicates": "Find duplicates",
    "panel.copies": "{n} copies:",
    "panel.noDuplicates": "No duplicate screenshots found.",
    "dups.deleteSelected": "Delete selected files",
    "dups.select": "Select {name}",
    "dups.nothingSelected": "Select at least one file",
    "confirm.deleteTitle": "Are you sure?",
    "confirm.deleteText": "Selected files ({n}) will be moved to the Recycle Bin and removed from the index.",
    "confirm.cancel": "Cancel",
    "confirm.delete": "Delete",
    "toast.deleted": "Deleted: {n}",
    "panel.privacy": "Privacy",
    "panel.filter": "filter",
    "panel.on": "on",
    "panel.off": "off",
    "panel.private": "private",
    "panel.purge": "remove",
    "panel.engines": "Engines",
    "panel.text": "text",
    "panel.images": "images",
    "panel.noClip": "no CLIP",
    "panel.noClipHint": "text-to-image search is unavailable without CLIP",
    "panel.notFound": "not found",
    "panel.privacyFilter": "Privacy filter",
    "panel.privacyAll": "All filters",
    "panel.privacySome": "{n} of {total} filters",
    "panel.privacyOff": "Filter is off",
    "panel.privacyNote": "Applied on the next index; “remove” deletes what is already indexed.",
    "privacy.keys": "Keys & SSH",
    "privacy.passwords": "Password managers",
    "privacy.wallets": "Crypto wallets",
    "privacy.browser": "Browser data",
    "privacy.finance": "Bank statements",
    "privacy.medical": "Medical records",
    "privacy.secrets": "Secrets in names & configs",

    "hints.focus": "focus",
    "hints.select": "select",
    "hints.preview": "preview",
    "hints.open": "open",
    "hints.window": "window",

    "toast.watchOn": "Watching the folders",
    "toast.watchOff": "Watching is off",
    "toast.purged": "Removed from the index: {n}",
    "toast.privacySaved": "Privacy filters updated",
    "toast.failed": "Failed: {error}",
    "toast.needRoots": "Pick at least one folder",
    "toast.jobDone": "done in {seconds} s · {files}",

    "settings.title": "Settings",
    "settings.close": "close",
    "settings.appearance": "Appearance",
    "settings.background": "Background",
    "settings.chooseImage": "choose an image…",
    "settings.wallNote": "The colour scheme comes out of the picture: one hue for every surface, so the window follows the background instead of sitting on top of it.",
    "settings.wallSaved": "Background updated",
    "settings.wallFailed": "Could not use that image: {error}",
    "settings.wallTooBig": "That image is larger than {mb} MB",
    "settings.zoom": "Zoom",
    "settings.wallEdit": "Framing…",
    "settings.wallEditor": "Framing",
    "settings.wallReset": "reset",
    "settings.wallEditorNote": "Drag the picture to choose the focus point; zoom with the slider or the mouse wheel. The focus stays put while you zoom.",
    "wall.forest": "Forest",
    "wall.dusk": "Dusk",
    "wall.slate": "Slate",
    "wall.sand": "Sand",
    "wall.ocean": "Ocean",
    "settings.language": "Language",
    "settings.languageNote": "Interface only. The index is language-agnostic — it reads every installed script either way.",
    "settings.about": "About",
    "settings.aboutFiles": "Files",
    "settings.aboutSize": "Database",
    "settings.aboutLanguages": "OCR languages",
    "settings.aboutEmbedder": "Text embedder",
    "settings.aboutImages": "Image embedder",
    "settings.aboutOcr": "OCR engine",
    "settings.aboutVersion": "Version",
    "settings.replayWelcome": "Show the welcome screen again",

    "onboard.title": "Welcome to AutoFind",
    "onboard.subtitle": "A quick setup before the first search. AutoFind reads files on this computer and sends nothing anywhere.",
    "onboard.language": "Interface language",
    "onboard.languageNote": "The welcome screen follows your system language. You can switch it here or later in settings.",
    "onboard.setupAutostart": "Start with Windows",
    "onboard.autostartNote": "Open AutoFind quietly in the tray when you sign in. Press Ctrl+Alt+Space to bring the window back.",
    "onboard.autostartOn": "Registered to start at login",
    "onboard.autostartOff": "Not registered",
    "onboard.autostartUnsupported": "Not available on this system",
    "onboard.autostartFailed": "Could not change the login entry: {error}",
    "onboard.importTitle": "Switch from Kensaku",
    "onboard.importNote": "Bring over pinned files, saved searches and dictionary words from a Kensaku install. Nothing is written until you press Import.",
    "onboard.importButton": "Import",
    "onboard.importScan": "Look for Kensaku",
    "onboard.importScanning": "looking…",
    "onboard.importFound": "Found {pins} pins · {saved} saved searches · {aliases} dictionary words.",
    "onboard.importNothing": "No Kensaku data was found on this computer.",
    "onboard.importDone": "Imported {pins} pins, {saved} searches and {aliases} words.",
    "onboard.importMissing": "{n} pinned files are not indexed yet — start indexing below.",
    "onboard.importFailed": "Import failed: {error}",
    "onboard.indexTitle": "Index your folders",
    "onboard.indexNote": "These folders are read on this machine. You can change the list later in the index panel.",
    "onboard.indexButton": "Start indexing",
    "onboard.indexRunning": "Indexing… {done}%",
    "onboard.indexEmpty": "No folders found — add them in the index panel later.",
    "onboard.indexDone": "{files} in {seconds} s",
    "onboard.indexFailed": "Indexing failed: {error}",
    "onboard.start": "Start searching",
    "onboard.skip": "Skip for now",
    "onboard.ready": "Ready — everything stays on this computer",
    "onboard.tagline": "Search the contents of documents and the text inside screenshots.",
    "onboard.footer": "No account. No cloud. No telemetry.",

    "alias.title": "Dictionary",
    "alias.note": "Your own words for what the files contain. Type `cv` and the search also looks for `резюме`. Whole words only, and never inside a filter like `type:cv`.",
    "alias.word": "word",
    "alias.means": "means",
    "alias.add": "Add",
    "alias.empty": "no words yet",
    "alias.added": "{alias} → {expand}",
    "alias.removed": "Removed {alias}",
    "alias.need": "Both a word and a meaning are needed",
    "alias.oneWord": "The word must be one word",

    "unit.kb": "KB",
    "unit.mb": "MB",
    "unit.seconds": "s",
    "unit.ms": "ms",
    "unit.files.one": "{n} file",
    "unit.files.few": "{n} files",
    "unit.files.many": "{n} files",
    "unit.chunks.one": "{n} chunk",
    "unit.chunks.few": "{n} chunks",
    "unit.chunks.many": "{n} chunks",
  },

  ru: {
    "app.title": "AutoFind — локальный поиск по файлам и скриншотам",
    "win.hide": "Скрыть окно (Ctrl+Alt+Space вернёт его)",
    "win.quit": "Выйти из AutoFind",
    "suggest.recent": "недавний",
    "suggest.saved": "сохранённый",

    "boot.starting": "запускаю",
    "boot.index": "открываю индекс",
    "boot.warm": "грею векторы",
    "boot.wait": "жду сервер",
    "boot.ready": "готово · {ms} мс",
    "boot.skip": "клик или Esc — пропустить",

    "search.placeholder": "поиск",
    "search.aria": "Поиск по файлам и скриншотам",
    "search.clear": "Очистить",
    "search.failed": "Поиск не удался: {error}",
    "search.meta": "{shown} из {sign}{total} · {ms} мс",

    "kind.all": "Всё",
    "kind.docs": "Документы",
    "kind.code": "Код",
    "kind.pdf": "PDF",
    "kind.docx": "DOCX",
    "kind.image": "Скриншоты",

    "tools.folder": "папка",
    "tools.any": "любая",
    "tools.since": "с",
    "tools.sort": "сортировка",
    "tools.status": "Состояние индекса",
    "sort.best": "по совпадению",
    "sort.newest": "новые",
    "sort.oldest": "старые",
    "sort.largest": "крупные",
    "sort.name": "по имени",

    "cal.open": "Выбрать дату",
    "cal.prev": "Предыдущий месяц",
    "cal.next": "Следующий месяц",
    "cal.clear": "сбросить",
    "cal.today": "сегодня",
    "cal.docDate": "искать по дате из документа, а не по дате файла",
    "cal.monthYear": "{month} {year}",
    "cal.months": "Январь,Февраль,Март,Апрель,Май,Июнь,Июль,Август,Сентябрь,Октябрь,Ноябрь,Декабрь",
    "cal.weekdays": "Пн,Вт,Ср,Чт,Пт,Сб,Вс",

    "status.checking": "проверяю индекс…",
    "status.files.one": "{n} файл в индексе",
    "status.files.few": "{n} файла в индексе",
    "status.files.many": "{n} файлов в индексе",
    "status.empty": "индекс пуст",
    "status.down": "сервер не отвечает",

    "idle.title": "Ищите по смыслу, а не по имени файла",
    "idle.body": "Строка находит содержимое документов, текст на скриншотах и фрагменты кода. Всё считается на этом компьютере, наружу ничего не уходит.",
    "idle.recent": "недавние скриншоты",
    "idle.tip": "подсказка: sym:parse_query находит определение, а не файл",
    "idle.pinned": "закреплённые",
    "idle.saved": "сохранённые поиски",
    "idle.pinsEmpty": "пока ничего не закреплено — нажмите звёздочку на любом результате",
    "idle.savedEmpty": "пока нет сохранённых поисков — введите запрос и нажмите закладку",

    "card.open": "открыть",
    "card.folder": "папка",
    "card.preview": "просмотр",
    "card.similar": "похожие",
    "card.showAll": "показать все {n}",
    "card.pin": "закрепить файл",
    "card.unpin": "открепить файл",
    "card.symbol": "определение",

    "pin.added": "Закреплено",
    "pin.removed": "Откреплено",

    "saved.save": "сохранить поиск",
    "saved.done": "Сохранено: {query}",
    "saved.failed": "Не удалось сохранить: {error}",
    "saved.forget": "забыть",
    "saved.forgotten": "Забыто",

    "group.snapshot.one": "снимок",
    "group.snapshot.few": "снимка",
    "group.snapshot.many": "снимков",
    "group.copy.one": "копия",
    "group.copy.few": "копии",
    "group.copy.many": "копий",
    "group.version.one": "версия",
    "group.version.few": "версии",
    "group.version.many": "версий",
    "group.failed": "не удалось загрузить",

    "results.more": "показать ещё",
    "results.similarImages": "похожие изображения",
    "results.nothing": "Ничего не найдено",
    "results.nothingHint": "Попробуйте другие слова, снимите фильтр по типу или проиндексируйте папку в панели справа.",

    "chip.time": "изменён: {value}",
    "chip.yesterday": "изменён: вчера",
    "chip.today": "изменён: сегодня",
    "chip.dayBefore": "изменён: позавчера",
    "chip.week": "изменён: за неделю",
    "chip.month": "изменён: за месяц",
    "chip.year": "изменён: за год",
    "chip.weekday": "изменён: {value}",
    "chip.daysAgo": "изменён: {value} дн. назад",
    "chip.lastDays": "изменён: за {value} дн.",
    "chip.lastWeeks": "изменён: за {value} нед.",
    "chip.lastMonths": "изменён: за {value} мес.",
    "chip.size": "размер: {value}",
    "chip.sizeMax": "размер до {value}",
    "chip.sizeMin": "размер от {value}",
    "chip.sizeLarge": "крупные файлы",
    "chip.sizeSmall": "мелкие файлы",
    "chip.type": "тип: {value}",
    "chip.ext": "расширение: .{value}",
    "chip.lang": "язык: {value}",
    "chip.sort": "сортировка: {value}",
    "chip.place": "в папке: {value}",
    "chip.symbol": "определение: {value}",
    "chip.exclude": "без «{value}»",
    "chip.filter": "regex {value}",
    "chip.remove": "убрать «{source}»",

    "typeVal.image": "картинки",
    "typeVal.doc": "документ",
    "typeVal.sheet": "таблица",
    "typeVal.presentation": "презентация",
    "typeVal.pdf": "pdf",

    "weekday.monday": "в понедельник",
    "weekday.tuesday": "во вторник",
    "weekday.wednesday": "в среду",
    "weekday.thursday": "в четверг",
    "weekday.friday": "в пятницу",
    "weekday.saturday": "в субботу",
    "weekday.sunday": "в воскресенье",

    "hint.unknown_lang": "неизвестный язык «{value}»",
    "hint.folders_not_indexed": "папки в индекс не попадают — ищите файлы",
    "hint.bad_modified": "не понял «modified:{value}» — нужно <7d, >30d или 7d..30d",
    "hint.bad_size": "не понял «size:{value}» — нужно >2mb, <100kb или 1mb..10mb",
    "hint.did_you_mean": 'вы имели в виду «{value}»? — нажмите, чтобы искать',
    "ctx.open": "Открыть",
    "ctx.reveal": "Показать в папке",
    "ctx.copy": "Копировать путь",
    "ctx.copied": "Путь скопирован",
    "ctx.copyFailed": "Не удалось скопировать путь",

    "preview.title": "Просмотр",
    "preview.close": "закрыть",
    "preview.opened": "открывали {n} раз",
    "preview.never": "ни разу не открывали",
    "preview.missing": "файла нет на диске",
    "preview.noText": "текст не извлекался",
    "preview.failed": "Не удалось открыть просмотр: {error}",

    "panel.toggle": "Панель индекса",
    "panel.index": "Индекс",
    "panel.files": "файлов",
    "panel.chunks": "фрагментов",
    "panel.screenshots": "скриншотов",
    "panel.withOcr": "с OCR",
    "panel.size": "размер",
    "panel.definitions": "определений",
    "panel.indexing": "Индексировать",
    "panel.rootsPlaceholder": "По одной папке в строке",
    "panel.prune": "удалять пропавшие",
    "panel.start": "Индексировать",
    "panel.watch": "Следить",
    "panel.ready": "готов к работе",
    "panel.watchingOff": "слежение выключено",
    "panel.watchOn": "слежение: {added} обновлено · {removed} удалено · {batch} в последней пачке",
    "panel.watchError": "слежение остановлено: {error}",
    "panel.duplicates": "Дубликаты скриншотов",
    "panel.duplicatesNote": "Только точные копии: одинаковое имя, размер и байты. Лишние можно выбрать здесь и удалить.",
    "panel.findDuplicates": "Найти дубликаты",
    "panel.copies": "копий: {n}",
    "panel.noDuplicates": "Дубликатов не найдено.",
    "dups.deleteSelected": "Удалить выбранные файлы",
    "dups.select": "Выбрать {name}",
    "dups.nothingSelected": "Выберите хотя бы один файл",
    "confirm.deleteTitle": "Вы уверены?",
    "confirm.deleteText": "Выбранные файлы ({n}) будут перемещены в корзину и убраны из индекса.",
    "confirm.cancel": "Отмена",
    "confirm.delete": "Удалить",
    "toast.deleted": "Удалено: {n}",
    "panel.privacy": "Приватность",
    "panel.filter": "фильтр",
    "panel.on": "включён",
    "panel.off": "выключен",
    "panel.private": "приватного",
    "panel.purge": "убрать",
    "panel.engines": "Движки",
    "panel.text": "текст",
    "panel.images": "картинки",
    "panel.noClip": "без CLIP",
    "panel.noClipHint": "без CLIP поиск по описанию недоступен",
    "panel.notFound": "не найден",
    "panel.privacyFilter": "Фильтр приватности",
    "panel.privacyAll": "Все фильтры",
    "panel.privacySome": "{n} из {total} фильтров",
    "panel.privacyOff": "Фильтр выключен",
    "panel.privacyNote": "Применяются при следующей индексации; «убрать» удаляет уже проиндексированное.",
    "privacy.keys": "Ключи и SSH",
    "privacy.passwords": "Менеджеры паролей",
    "privacy.wallets": "Криптокошельки",
    "privacy.browser": "Данные браузеров",
    "privacy.finance": "Банковские выписки",
    "privacy.medical": "Медкарты",
    "privacy.secrets": "Секреты в именах и конфигах",

    "hints.focus": "фокус",
    "hints.select": "выбор",
    "hints.preview": "просмотр",
    "hints.open": "открыть",
    "hints.window": "окно",

    "toast.watchOn": "Слежу за папками",
    "toast.watchOff": "Слежение выключено",
    "toast.purged": "Убрано из индекса: {n}",
    "toast.privacySaved": "Фильтры приватности обновлены",
    "toast.failed": "Не удалось: {error}",
    "toast.needRoots": "Укажите хотя бы одну папку",
    "toast.jobDone": "готово за {seconds} с · {files}",

    "settings.title": "Настройки",
    "settings.close": "закрыть",
    "settings.appearance": "Оформление",
    "settings.background": "Фон",
    "settings.chooseImage": "выбрать картинку…",
    "settings.wallNote": "Цветовая гамма выводится из картинки: один оттенок на все поверхности, поэтому окно подстраивается под фон, а не спорит с ним.",
    "settings.wallSaved": "Фон обновлён",
    "settings.wallFailed": "Не удалось использовать картинку: {error}",
    "settings.wallTooBig": "Картинка больше {mb} МБ",
    "settings.zoom": "Масштаб",
    "settings.wallEdit": "Кадрирование…",
    "settings.wallEditor": "Кадрирование",
    "settings.wallReset": "сброс",
    "settings.wallEditorNote": "Перетащите картинку, чтобы выбрать фокус; масштаб — ползунком или колесом мыши. При зуме фокус остаётся на месте.",
    "wall.forest": "Лес",
    "wall.dusk": "Сумерки",
    "wall.slate": "Графит",
    "wall.sand": "Песок",
    "wall.ocean": "Океан",
    "settings.language": "Язык",
    "settings.languageNote": "Только интерфейс. Индекс от языка не зависит — он читает все установленные письменности в любом случае.",
    "settings.about": "О программе",
    "settings.aboutFiles": "Файлов",
    "settings.aboutSize": "База",
    "settings.aboutLanguages": "Языков OCR",
    "settings.aboutEmbedder": "Текстовый эмбеддер",
    "settings.aboutImages": "Эмбеддер картинок",
    "settings.aboutOcr": "Движок OCR",
    "settings.aboutVersion": "Версия",
    "settings.replayWelcome": "Показать приветствие снова",

    "onboard.title": "Добро пожаловать в AutoFind",
    "onboard.subtitle": "Короткая настройка перед первым поиском. AutoFind читает файлы на этом компьютере и никуда их не отправляет.",
    "onboard.language": "Язык интерфейса",
    "onboard.languageNote": "Экран приветствия подстраивается под язык системы. Переключить его можно здесь или позже в настройках.",
    "onboard.setupAutostart": "Запускать с Windows",
    "onboard.autostartNote": "Тихо открывать AutoFind в трее при входе в систему. Вернуть окно — Ctrl+Alt+Space.",
    "onboard.autostartOn": "Добавлено в автозапуск",
    "onboard.autostartOff": "Не добавлено в автозапуск",
    "onboard.autostartUnsupported": "Недоступно в этой системе",
    "onboard.autostartFailed": "Не удалось изменить автозапуск: {error}",
    "onboard.importTitle": "Переезд с Kensaku",
    "onboard.importNote": "Перенесите закреплённые файлы, сохранённые запросы и слова из словаря Kensaku. Ничего не запишется, пока вы не нажмёте «Импортировать».",
    "onboard.importButton": "Импортировать",
    "onboard.importScan": "Найти Kensaku",
    "onboard.importScanning": "ищу…",
    "onboard.importFound": "Найдено: закреплённых файлов — {pins} · сохранённых запросов — {saved} · слов — {aliases}.",
    "onboard.importNothing": "Данные Kensaku на этом компьютере не найдены.",
    "onboard.importDone": "Импортировано: файлов — {pins}, запросов — {saved}, слов — {aliases}.",
    "onboard.importMissing": "{n} закреплённых файлов ещё не в индексе — начните индексацию ниже.",
    "onboard.importFailed": "Не удалось импортировать: {error}",
    "onboard.indexTitle": "Проиндексировать папки",
    "onboard.indexNote": "Эти папки читаются на этом компьютере. Список можно изменить позже в панели индекса.",
    "onboard.indexButton": "Начать индексацию",
    "onboard.indexRunning": "Индексация… {done}%",
    "onboard.indexEmpty": "Папки не найдены — добавьте их позже в панели индекса.",
    "onboard.indexDone": "{files} за {seconds} с",
    "onboard.indexFailed": "Индексация не удалась: {error}",
    "onboard.start": "Начать поиск",
    "onboard.skip": "Пропустить",
    "onboard.ready": "Готово — всё остаётся на этом компьютере",
    "onboard.tagline": "Поиск по содержимому документов и тексту на скриншотах.",
    "onboard.footer": "Без аккаунта. Без облака. Без телеметрии.",

    "alias.title": "Словарь",
    "alias.note": "Ваши слова вместо тех, что в файлах. Наберёте `cv` — поиск посмотрит и `резюме`. Только целые слова и никогда внутри фильтра вроде `type:cv`.",
    "alias.word": "слово",
    "alias.means": "значит",
    "alias.add": "Добавить",
    "alias.empty": "пока пусто",
    "alias.added": "{alias} → {expand}",
    "alias.removed": "Удалено: {alias}",
    "alias.need": "Нужны и слово, и его значение",
    "alias.oneWord": "Слово должно быть одним словом",

    "unit.kb": "КБ",
    "unit.mb": "МБ",
    "unit.seconds": "с",
    "unit.ms": "мс",
    "unit.files.one": "{n} файл",
    "unit.files.few": "{n} файла",
    "unit.files.many": "{n} файлов",
    "unit.chunks.one": "{n} фрагмент",
    "unit.chunks.few": "{n} фрагмента",
    "unit.chunks.many": "{n} фрагментов",
  },
};

const LANGUAGES = [
  { code: "en", label: "English" },
  { code: "ru", label: "Русский" },
];

const LANG_KEY = "autofind.lang";
const DEFAULT_LANG = "en";

function detectLang() {
  let stored = null;
  try {
    stored = localStorage.getItem(LANG_KEY);
  } catch (error) {
    // A blocked storage must not decide the language: fall through to the
    // system language, and then to English.
  }
  if (I18N[stored]) return stored;

  // First run: the interface should already speak the language of the machine.
  // Only English and Russian are translated, so everything else gets English —
  // which is a better first impression than a Russian interface on a German
  // desktop. The switcher stays visible either way.
  let system = "";
  try {
    system = (navigator.language || (navigator.languages || [])[0] || "").toLowerCase();
  } catch (error) {
    system = "";
  }
  const base = system.split("-")[0];
  return I18N[base] ? base : DEFAULT_LANG;
}

let LANG = detectLang();

function currentLang() {
  return LANG;
}

function t(key, vars) {
  const table = I18N[LANG] || I18N[DEFAULT_LANG];
  let text = table[key] ?? I18N[DEFAULT_LANG][key] ?? key;
  if (vars) {
    text = text.replace(/\{(\w+)\}/g, (match, name) => (
      Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : match
    ));
  }
  return text;
}

/* Russian needs three forms and English two. The rule lives here, next to the
   strings, because it is a property of the language and not of the count. */
function plural(count, key, vars) {
  let form;
  if (LANG === "ru") {
    const tail = count % 100;
    const last = count % 10;
    if (tail >= 11 && tail <= 14) form = "many";
    else if (last === 1) form = "one";
    else if (last >= 2 && last <= 4) form = "few";
    else form = "many";
  } else {
    form = count === 1 ? "one" : "many";
  }
  return t(`${key}.${form}`, { ...vars, n: count });
}

function setLang(code) {
  if (!I18N[code] || code === LANG) return false;
  LANG = code;
  try {
    localStorage.setItem(LANG_KEY, code);
  } catch (error) {
    // Private mode: the choice lasts for this session, which is better than
    // refusing to switch at all.
  }
  applyStaticText();
  return true;
}

/* Static markup carries `data-i18n` keys; everything rendered by app.js calls
   `t()` directly. Both paths end up here so a language switch is one call. */
function applyStaticText() {
  document.documentElement.lang = LANG;
  document.title = t("app.title");

  document.querySelectorAll("[data-i18n]").forEach((node) => {
    node.textContent = t(node.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    node.placeholder = t(node.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    node.title = t(node.dataset.i18nTitle);
  });
  document.querySelectorAll("[data-i18n-aria]").forEach((node) => {
    node.setAttribute("aria-label", t(node.dataset.i18nAria));
  });
}

/* Called by the tests and available from the console: a key that exists in one
   language and not another is the bug this file is meant to prevent. */
function checkTranslations() {
  const reference = Object.keys(I18N[DEFAULT_LANG]);
  const problems = [];
  for (const code of Object.keys(I18N)) {
    const keys = Object.keys(I18N[code]);
    for (const key of reference) {
      if (!keys.includes(key)) problems.push(`${code}: missing ${key}`);
    }
    for (const key of keys) {
      if (!reference.includes(key)) problems.push(`${code}: extra ${key}`);
    }
  }
  return problems;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { I18N, LANGUAGES, t, plural, setLang, currentLang, checkTranslations };
}
