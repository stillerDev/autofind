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
    "idle.pinsEmpty": "nothing pinned yet — hover a result and press the star",
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

    "hints.focus": "focus",
    "hints.select": "select",
    "hints.preview": "preview",
    "hints.open": "open",
    "hints.window": "window",

    "toast.watchOn": "Watching the folders",
    "toast.watchOff": "Watching is off",
    "toast.purged": "Removed from the index: {n}",
    "toast.failed": "Failed: {error}",
    "toast.needRoots": "Pick at least one folder",
    "toast.jobDone": "done in {seconds} s · {files}",

    "settings.title": "Settings",
    "settings.close": "close",
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
    "idle.pinsEmpty": "пока ничего не закреплено — наведите на результат и нажмите звёздочку",
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

    "hints.focus": "фокус",
    "hints.select": "выбор",
    "hints.preview": "просмотр",
    "hints.open": "открыть",
    "hints.window": "окно",

    "toast.watchOn": "Слежу за папками",
    "toast.watchOff": "Слежение выключено",
    "toast.purged": "Убрано из индекса: {n}",
    "toast.failed": "Не удалось: {error}",
    "toast.needRoots": "Укажите хотя бы одну папку",
    "toast.jobDone": "готово за {seconds} с · {files}",

    "settings.title": "Настройки",
    "settings.close": "закрыть",
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
    // A blocked storage must not decide the language: fall through to default.
  }
  return I18N[stored] ? stored : DEFAULT_LANG;
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
