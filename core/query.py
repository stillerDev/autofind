"""Query grammar and natural-language intent.

Two jobs, both deterministic and both local:

1. **Grammar.** `type:pdf`, `ext:rs`, `in:"C:\\Docs"`, `path:src`, `modified:<7d`,
   `size:>2mb`, `is:image`, `lang:python`, `name:config`, `content:timeout`,
   `re:"TODO|FIXME"`, `-node_modules`, `"exact phrase"`, `mode:exhaustive`,
   `sort:newest`. Filters narrow the set; they never replace the text.

2. **Intent.** Ordinary phrasing — «скриншоты за прошлую неделю», «самые большие
   файлы», «3 дня назад», "latest invoice" — compiled into the same grammar.
   Every inference is returned as a chip with the words that produced it, so the
   user can see and remove it. No model, no network, no silent rewrites.

The shape is borrowed from kensaku (MIT): explicit grammar is authoritative, so
intent compilation is skipped entirely once the query already contains a filter.
Unknown `key:value` falls back to a literal term instead of returning nothing.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase

# ---------------------------------------------------------------- vocabularies

# Extension -> kind is decided at scan time and stored per file. These groups
# are what `type:` means: a kind when the kind exists, an extension set when the
# category is broader than one extractor (sheets, presentations, archives).
TYPE_KINDS = {
    "image": {"image"},
    "img": {"image"},
    "doc": {"pdf", "docx", "text"},
    "document": {"pdf", "docx", "text"},
    "text": {"text"},
    "txt": {"text"},
    "code": {"code"},
    "pdf": {"pdf"},
    "docx": {"docx"},
    "epub": {"epub"},
    "rtf": {"rtf"},
    "odt": {"odt"},
    "pptx": {"pptx"},
    "xlsx": {"xlsx"},
    "email": {"email"},
    "mail": {"email"},
    "archive": {"archive"},
}
TYPE_EXTS = {
    "sheet": {".xlsx", ".xls", ".ods", ".csv", ".tsv"},
    "spreadsheet": {".xlsx", ".xls", ".ods", ".csv", ".tsv"},
    "presentation": {".pptx", ".ppt", ".odp"},
    "slides": {".pptx", ".ppt", ".odp"},
    "archive": {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"},
    "video": {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"},
    "audio": {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"},
}
LANG_EXT = {
    "rust": {".rs"},
    "python": {".py", ".pyi"},
    "javascript": {".js", ".mjs", ".cjs", ".jsx"},
    "typescript": {".ts", ".tsx"},
    "go": {".go"},
    "java": {".java"},
    "kotlin": {".kt", ".kts"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hh"},
    "csharp": {".cs"},
    "ruby": {".rb"},
    "php": {".php"},
    "shell": {".sh", ".bash", ".zsh", ".ps1"},
    "sql": {".sql"},
    "html": {".html", ".htm"},
    "css": {".css", ".scss", ".sass"},
    "swift": {".swift"},
    "lua": {".lua"},
    "vue": {".vue"},
    "svelte": {".svelte"},
    "json": {".json"},
    "yaml": {".yaml", ".yml"},
    "toml": {".toml"},
    "markdown": {".md", ".markdown"},
}
SORTS = {"newest", "oldest", "largest", "name", "best"}

_DURATION_UNITS = {
    "": 1.0, "s": 1.0, "sec": 1.0, "secs": 1.0, "с": 1.0, "сек": 1.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "мин": 60.0,
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0, "ч": 3600.0,
    "d": 86400.0, "day": 86400.0, "days": 86400.0, "д": 86400.0, "дн": 86400.0, "дней": 86400.0,
    "w": 604800.0, "wk": 604800.0, "week": 604800.0, "weeks": 604800.0, "н": 604800.0,
    "y": 31536000.0, "yr": 31536000.0, "year": 31536000.0, "years": 31536000.0, "г": 31536000.0,
}
_SIZE_UNITS = {
    "": 1.0, "b": 1.0, "б": 1.0,
    "k": 1024.0, "kb": 1024.0, "kib": 1024.0, "к": 1024.0, "кб": 1024.0,
    "m": 1048576.0, "mb": 1048576.0, "mib": 1048576.0, "м": 1048576.0, "мб": 1048576.0,
    "g": 1073741824.0, "gb": 1073741824.0, "gib": 1073741824.0, "гб": 1073741824.0,
}


# -------------------------------------------------------------------- pieces

@dataclass(slots=True)
class Range:
    """One numeric window. `None` means unbounded on that side."""

    low: float | None = None
    high: float | None = None

    def matches(self, value: float) -> bool:
        if self.low is not None and value < self.low:
            return False
        if self.high is not None and value > self.high:
            return False
        return True


@dataclass(slots=True)
class Term:
    text: str
    field: str = "any"  # any | name | content | path
    phrase: bool = False


# The words around a chip are a language; the chip itself is not. A chip is a
# kind plus a value ("time" + "7d"), and whoever draws it decides how to say
# that. The first version baked a Russian label into the parser, which meant the
# interface could not be translated without string surgery on the search engine.
CHIP_TEMPLATES: dict[str, str] = {
    "time": "modified: {value}",
    "yesterday": "modified: yesterday",
    "today": "modified: today",
    "dayBefore": "modified: the day before yesterday",
    "week": "modified: in the past week",
    "month": "modified: in the past month",
    "year": "modified: in the past year",
    "weekday": "modified: {value}",
    "daysAgo": "modified: {value} days ago",
    "lastDays": "modified: last {value} days",
    "lastWeeks": "modified: last {value} weeks",
    "lastMonths": "modified: last {value} months",
    "size": "size: {value}",
    "sizeMax": "size under {value}",
    "sizeMin": "size over {value}",
    "sizeLarge": "large files",
    "sizeSmall": "small files",
    "type": "type: {value}",
    "ext": "extension: .{value}",
    "lang": "language: {value}",
    "sort": "sort: {value}",
    "symbol": "symbol: {value}",
    "place": "in folder: {value}",
    "filter": "regex {value}",
    "exclude": "without “{value}”",
    "tag": "tag: {value}",
    "collection": "collection: {value}",
}

WEEKDAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@dataclass(slots=True)
class Chip:
    """One interpretation the user is allowed to take back."""

    kind: str  # see CHIP_TEMPLATES
    value: str
    raw: str
    source: str

    @property
    def label(self) -> str:
        """English rendering, for the CLI and for anything without a locale."""
        if self.kind == "weekday":
            value = f"last {WEEKDAY_NAMES[int(self.value)]}"
        else:
            value = self.value
        return CHIP_TEMPLATES.get(self.kind, "{value}").format(value=value)

    def as_dict(self) -> dict:
        return {
            "kind": self.kind,
            "value": self.value,
            "label": self.label,
            "raw": self.raw,
            "source": self.source,
        }


HINT_TEMPLATES: dict[str, str] = {
    "unknown_lang": "unknown language “{value}”",
    "folders_not_indexed": "folders are not indexed — search for files",
    "bad_modified": "did not understand “modified:{value}” — use <7d, >30d or 7d..30d",
    "bad_size": "did not understand “size:{value}” — use >2mb, <100kb or 1mb..10mb",
    "did_you_mean": 'did you mean “{value}”?',
}


@dataclass(slots=True)
class Hint:
    """A thing the compiler could not read, said in a language-neutral way."""

    kind: str
    value: str = ""

    @property
    def text(self) -> str:
        return HINT_TEMPLATES.get(self.kind, "{value}").format(value=self.value)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "text": self.text}


@dataclass(slots=True)
class ParsedQuery:
    raw: str = ""
    terms: list[Term] = field(default_factory=list)
    kinds: set[str] = field(default_factory=set)
    exts: set[str] = field(default_factory=set)
    # A user-defined type: basename globs such as `*.jpg`. Empty means the
    # chosen type exists but constrains nothing.
    masks: list[str] = field(default_factory=list)
    in_paths: list[str] = field(default_factory=list)
    not_paths: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    contents: list[str] = field(default_factory=list)
    regexes: list[str] = field(default_factory=list)
    # `sym:name` — a definition, not a mention. Served by the symbols table.
    symbols: list[str] = field(default_factory=list)
    # The user's own labels. Tags are AND; collections are a union (a file can
    # live in several, and asking for two collections means "either").
    tags: list[str] = field(default_factory=list)
    collections: list[str] = field(default_factory=list)
    modified: Range | None = None
    size: Range | None = None
    mode: str = "ranked"
    sort: str | None = None
    chips: list[Chip] = field(default_factory=list)
    hints: list[Hint] = field(default_factory=list)

    # -- what the retrieval channels see ---------------------------------
    @property
    def text(self) -> str:
        """Free text for BM25 and the embedder. Regex and field terms excluded."""
        return " ".join(term.text for term in self.terms if term.field == "any")

    @property
    def name_text(self) -> str:
        return " ".join(term.text for term in self.terms if term.field == "name")

    @property
    def content_text(self) -> str:
        return " ".join(term.text for term in self.terms if term.field == "content")

    @property
    def has_filters(self) -> bool:
        return bool(
            self.kinds or self.exts or self.masks or self.in_paths or self.not_paths
            or self.names or self.contents or self.regexes or self.symbols
            or self.tags or self.collections
            or self.modified or self.size
        )

    @property
    def is_empty(self) -> bool:
        return not (self.terms or self.regexes or self.has_filters)

    # -- row predicate ---------------------------------------------------
    def matches(
        self, path: str, kind: str, size: int, mtime: float,
        skip_modified: bool = False,
    ) -> bool:
        lowered = path.lower()
        if self.kinds and kind not in self.kinds:
            return False
        if self.exts:
            dot = lowered.rfind(".")
            slash = max(lowered.rfind("\\"), lowered.rfind("/"))
            if dot <= slash or lowered[dot:] not in self.exts:
                return False
        if self.masks:
            base = _basename(lowered)
            if not any(fnmatchcase(base, mask) for mask in self.masks):
                return False
        for prefix in self.in_paths:
            if not lowered.startswith(prefix.lower()):
                return False
        for fragment in self.not_paths:
            if fragment.lower() in lowered:
                return False
        # `skip_modified` serves the document-date mode: the caller has already
        # accepted this row on the strength of its claimed date instead of its
        # mtime, so the mtime range must not veto it a second time.
        if self.modified is not None and not skip_modified and not self.modified.matches(mtime):
            return False
        if self.size is not None and not self.size.matches(size):
            return False
        for fragment in self.names:
            if fragment.lower() not in _basename(lowered):
                return False
        return True


def _basename(path: str) -> str:
    return path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]


# ------------------------------------------------------------------- lexing

@dataclass(slots=True)
class Token:
    kind: str  # word | phrase | key | negated_word | negated_phrase | regex
    text: str = ""
    key: str = ""
    negated: bool = False
    quoted: bool = False


def _is_escape(text: str, index: int) -> bool:
    """Inside quotes only `\\"` and `\\\\` are escapes, so Windows paths survive."""
    return text[index] == "\\" and index + 1 < len(text) and text[index + 1] in ('"', "\\")


def tokenize(raw: str) -> list[Token]:
    tokens: list[Token] = []
    i, length = 0, len(raw)
    while i < length:
        while i < length and raw[i].isspace():
            i += 1
        if i >= length:
            break

        negated = raw[i] == "-" and i + 1 < length and not raw[i + 1].isspace()
        if negated:
            i += 1

        if i < length and raw[i] == '"':
            i += 1
            chunk: list[str] = []
            while i < length and raw[i] != '"':
                if _is_escape(raw, i):
                    i += 1
                chunk.append(raw[i])
                i += 1
            i += 1  # closing quote, if present
            text = "".join(chunk)
            tokens.append(Token("negated_phrase" if negated else "phrase", text, negated=negated))
            continue

        start = i
        colon = -1
        while i < length and not raw[i].isspace():
            if raw[i] == ":" and colon == -1:
                colon = i
                if i + 1 < length and raw[i + 1] == '"':
                    i += 1  # park on the quote so the branch below reads the value
                    break
            i += 1

        if colon != -1:
            key = raw[start:colon]
            if i < length and raw[i] == '"':
                i += 1
                chunk = []
                while i < length and raw[i] != '"':
                    if _is_escape(raw, i):
                        i += 1
                    chunk.append(raw[i])
                    i += 1
                i += 1
                value, quoted = "".join(chunk), True
            else:
                value, quoted = raw[colon + 1:i].strip("()"), False
            if key.lower() == "re":
                tokens.append(Token("regex", value, negated=negated))
            else:
                tokens.append(Token("key", value, key=key, negated=negated, quoted=quoted))
            continue

        word = raw[start:i].strip("()")
        if word:
            tokens.append(Token("negated_word" if negated else "word", word, negated=negated))
    return tokens


# ------------------------------------------------------------------ numbers

def _parse_duration(value: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Zа-яА-Я]*)\s*", value)
    if not match:
        return None
    unit = _DURATION_UNITS.get(match.group(2).lower())
    return None if unit is None else float(match.group(1)) * unit


def _parse_size(value: str) -> float | None:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Zа-яА-Я]*)\s*", value)
    if not match:
        return None
    unit = _SIZE_UNITS.get(match.group(2).lower())
    return None if unit is None else float(match.group(1)) * unit


def parse_modified(value: str, now: float | None = None) -> Range | None:
    """`<7d` = within a week, `>30d` = older than a month, `7d..30d` = the gap."""
    now = time.time() if now is None else now
    text = value.strip()
    if ".." in text:
        first, _, second = text.partition("..")
        left, right = _parse_duration(first), _parse_duration(second)
        if left is None or right is None:
            return None
        low, high = sorted((left, right))
        return Range(now - high, now - low)
    if text.startswith("<"):
        span = _parse_duration(text.lstrip("<="))
        return None if span is None else Range(now - span, None)
    if text.startswith(">"):
        span = _parse_duration(text.lstrip(">="))
        return None if span is None else Range(None, now - span)
    span = _parse_duration(text)
    return None if span is None else Range(now - span, None)


def parse_size(value: str) -> Range | None:
    text = value.strip()
    if ".." in text:
        first, _, second = text.partition("..")
        left, right = _parse_size(first), _parse_size(second)
        if left is None or right is None:
            return None
        low, high = sorted((left, right))
        return Range(low, high)
    for prefix, make in ((">=", lambda n: Range(n, None)), (">", lambda n: Range(n, None)),
                         ("<=", lambda n: Range(None, n)), ("<", lambda n: Range(None, n))):
        if text.startswith(prefix):
            number = _parse_size(text[len(prefix):])
            return None if number is None else make(number)
    exact = _parse_size(text)
    return None if exact is None else Range(max(0.0, exact - 1024), exact + 1024)


# ------------------------------------------------------------------ parsing

def parse(raw: str, now: float | None = None) -> ParsedQuery:
    query = ParsedQuery(raw=raw)
    for token in tokenize(raw):
        if token.kind == "word":
            query.terms.append(Term(token.text))
        elif token.kind == "phrase":
            query.terms.append(Term(token.text, phrase=True))
        elif token.kind == "negated_word":
            query.not_paths.append(token.text)
            query.chips.append(Chip("exclude", token.text, f"-{token.text}", token.text))
        elif token.kind == "negated_phrase":
            query.not_paths.append(token.text)
            query.chips.append(Chip("exclude", token.text, f'-"{token.text}"', token.text))
        elif token.kind == "regex":
            query.regexes.append(token.text)
            query.chips.append(Chip("filter", token.text, f're:"{token.text}"', token.text))
        elif token.kind == "key":
            _apply_key(query, token, now)
    return query


def _apply_key(query: ParsedQuery, token: Token, now: float | None) -> None:
    key = token.key.lower()
    value = token.text
    negated = token.negated
    literal = f'-{token.key}:{value}' if negated else f"{token.key}:{value}"

    if key in ("type", "ext", "is", "lang"):
        if key == "lang":
            exts = LANG_EXT.get(value.lower())
            if exts is None:
                query.hints.append(Hint("unknown_lang", value))
                query.terms.append(Term(literal))
                return
            query.exts |= exts
            query.chips.append(Chip("lang", value, literal, literal))
            return

        word = value.lower().lstrip(".")
        if key == "ext":
            query.exts.add("." + word)
            query.chips.append(Chip("ext", word, literal, literal))
            return

        if word in ("dir", "dirs", "folder", "folderа", "папка", "папки"):
            query.hints.append(Hint("folders_not_indexed"))
            query.kinds.add("__dir__")
            return
        if word in ("file", "files", "файл", "файлы"):
            return
        if word in TYPE_KINDS:
            query.kinds |= TYPE_KINDS[word]
            query.chips.append(Chip("type", value, literal, literal))
            return
        if word in TYPE_EXTS:
            query.exts |= TYPE_EXTS[word]
            query.chips.append(Chip("type", value, literal, literal))
            return
        # Unknown category: read it as an extension, which is what a user
        # typing `type:md` means anyway.
        query.exts.add("." + word)
        query.chips.append(Chip("ext", word, literal, literal))
        return

    if key in ("sym", "symbol", "def", "fn"):
        # A definition lookup, not a text search: `sym:parse_query` finds the
        # file that *defines* it, which is the question you actually have when
        # you remember a name and not where it lives.
        query.symbols.append(value)
        query.chips.append(Chip("symbol", value, literal, literal))
        return

    if key == "in":
        query.in_paths.append(value)
        query.chips.append(Chip("place", value, literal, literal))
        return

    if key == "path":
        query.terms.append(Term(value, field="path", phrase=token.quoted))
        return

    if key == "name":
        query.names.append(value)
        query.terms.append(Term(value, field="name", phrase=token.quoted))
        return

    if key == "content":
        query.contents.append(value)
        query.terms.append(Term(value, field="content", phrase=token.quoted))
        return

    if key in ("tag", "tags"):
        clean = " ".join(value.split())[:64]
        if clean:
            query.tags.append(clean)
            source = f'{token.key}:"{clean}"' if (" " in clean or token.quoted) else literal
            query.chips.append(Chip("tag", clean, source, source))
            return
        query.terms.append(Term(literal))
        return

    if key in ("collection", "col", "set"):
        clean = " ".join(value.split())[:96]
        if clean:
            query.collections.append(clean)
            source = f'{token.key}:"{clean}"' if (" " in clean or token.quoted) else literal
            query.chips.append(Chip("collection", clean, source, source))
            return
        query.terms.append(Term(literal))
        return

    if key == "modified":
        window = parse_modified(value, now)
        if window is None:
            query.hints.append(Hint("bad_modified", value))
            query.terms.append(Term(literal))
            return
        query.modified = window
        query.chips.append(Chip("time", value, literal, literal))
        return

    if key == "size":
        window = parse_size(value)
        if window is None:
            query.hints.append(Hint("bad_size", value))
            query.terms.append(Term(literal))
            return
        query.size = window
        query.chips.append(Chip("size", value, literal, literal))
        return

    if key == "mode":
        query.mode = "exhaustive" if value.lower() == "exhaustive" else "ranked"
        return

    if key == "sort":
        order = value.lower()
        if order in SORTS:
            query.sort = order
        return

    if key == "visual":
        query.kinds.add("image")
        query.terms.append(Term(value))
        query.chips.append(Chip("type", "image", "type:image", literal))
        return

    # Unknown key: keep it as text so a stray colon never empties the results.
    query.terms.append(Term(literal))


# ------------------------------------------------------------------- intent

_RU_WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "среду": 2, "среда": 2, "четверг": 3,
    "пятницу": 4, "пятница": 4, "субботу": 5, "суббота": 5, "воскресенье": 6,
}
_EN_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# Ordered longest-first so «позавчера» is not eaten by «вчера», and the leading
# preposition is part of the match so «за прошлую неделю» leaves no stray «за».
# Each row is (pattern, filter, chip kind, chip value): the kind and the value
# are the whole message, and the words around them belong to the interface.
_TIME_RULES: list[tuple[str, str, str]] = [
    (r"позавчера|day before yesterday", "modified:2d..3d", "dayBefore"),
    (r"\bвчерашн\w*\b|\bвчера\b|\byesterday\b", "modified:1d..2d", "yesterday"),
    (r"\bсегодняшн\w*\b|\bсегодня\b|\btoday\b", "modified:<1d", "today"),
    (r"(?:за|на|в)\s+(?:последн\w*\s+|прошл\w*\s+|эт\w*\s+)?недел\w*|"
     r"\b(?:last|this|past)\s+week\b", "modified:<7d", "week"),
    (r"(?:за|на|в)\s+(?:последн\w*\s+|прошл\w*\s+|эт\w*\s+)?месяц\w*|"
     r"\b(?:last|this|past)\s+month\b", "modified:<30d", "month"),
    (r"(?:за|на|в)\s+(?:последн\w*\s+|прошл\w*\s+|эт\w*\s+)?год\w*|"
     r"\b(?:last|this|past)\s+year\b", "modified:<365d", "year"),
]
_SORT_RULES: list[tuple[str, str, str]] = [
    (r"\b(?:последн\w*|новейш\w*|самы\w*\s+нов\w*|свеж\w*)\b|"
     r"\b(?:latest|newest|most recent)\b", "newest", "newest"),
    (r"\b(?:старейш\w*|самы\w*\s+стар\w*|самы\w*\s+давн\w*)\b|\boldest\b",
     "oldest", "oldest"),
    (r"\b(?:самы\w*\s+больш\w*|крупнейш\w*|самы\w*\s+тяжёл\w*)\b|"
     r"\b(?:biggest|largest)\b", "largest", "largest"),
]
_SIZE_RULES: list[tuple[str, str, str]] = [
    (r"\b(?:меньше|до|не\s+больше)\s+(\d+(?:[.,]\d+)?)\s*(кб|мб|гб|kb|mb|gb)\b|"
     r"\b(?:under|less than)\s+(\d+(?:\.\d+)?)\s*(kb|mb|gb)\b", "<", "sizeMax"),
    (r"\b(?:больше|свыше|от)\s+(\d+(?:[.,]\d+)?)\s*(кб|мб|гб|kb|mb|gb)\b|"
     r"\b(?:over|more than|larger than)\s+(\d+(?:\.\d+)?)\s*(kb|mb|gb)\b", ">", "sizeMin"),
    (r"\b(?:большие|крупные|огромные|тяжёлые|тяжелые)\b|\b(?:big|large|huge)\b",
     ">100mb", "sizeLarge"),
    (r"\b(?:маленькие|мелкие|крошечные|небольшие)\b|\b(?:small|tiny)\b",
     "<1mb", "sizeSmall"),
]
_TYPE_RULES: list[tuple[str, str, str]] = [
    (r"\bскриншот\w*\b|\bскрин\w*\b|\bscreenshots?\b|\bcaptures?\b", "image", "image"),
    (r"\bфотк\w*\b|\bфото\b|\bкартинк\w*\b|\bизображени\w*\b|\bimages?\b|\bphotos?\b",
     "image", "image"),
    (r"\bдокумент\w*\b|\bdocuments?\b|\bdocs?\b", "doc", "doc"),
    (r"\bтаблиц\w*\b|\bспредшит\w*\b|\bspreadsheets?\b", "sheet", "sheet"),
    (r"\bпрезентаци\w*\b|\bслайд\w*\b|\bslides?\b|\bpresentations?\b", "presentation",
     "presentation"),
    (r"\bpdf\b", "pdf", "pdf"),
]


def _compile_time_travel(text: str, now: float, appends: list[str], chips: list[Chip]) -> str:
    """«в прошлый вторник», «3 дня назад», «last 3 days» -> a one-day window."""
    weekday_pattern = (
        r"(?:в\s+)?прошл\w*\s+(" + "|".join(_RU_WEEKDAYS) + r")\b|"
        r"\blast\s+(" + "|".join(_EN_WEEKDAYS) + r")\b"
    )
    match = re.search(weekday_pattern, text, re.IGNORECASE)
    if match:
        name = (match.group(1) or match.group(2) or "").lower()
        target = _RU_WEEKDAYS.get(name, _EN_WEEKDAYS.get(name))
        if target is not None:
            today = datetime.fromtimestamp(now).weekday()
            back = (today - target) % 7 or 7
            appends.append(f"modified:{back}d..{back + 1}d")
            chips.append(Chip("weekday", str(target), f"modified:{back}d..{back + 1}d",
                              match.group(0)))
            text = text[:match.start()] + " " + text[match.end():]

    ago = re.search(r"\b(\d{1,3})\s*(?:дн\w*|дней|days?)\s+назад\b|\b(\d{1,3})\s+days?\s+ago\b",
                    text, re.IGNORECASE)
    if ago:
        days = int(ago.group(1) or ago.group(2))
        appends.append(f"modified:{days}d..{days + 1}d")
        chips.append(Chip("daysAgo", str(days), f"modified:{days}d..{days + 1}d",
                          ago.group(0)))
        text = text[:ago.start()] + " " + text[ago.end():]

    span = re.search(r"\bза\s+(?:последни\w*\s+)?(\d{1,3})\s*(дн\w*|недел\w*|месяц\w*)\b|"
                     r"\blast\s+(\d{1,3})\s*(days?|weeks?|months?)\b", text, re.IGNORECASE)
    if span:
        count = int(span.group(1) or span.group(3))
        unit = (span.group(2) or span.group(4) or "").lower()
        factor, kind = (
            (30, "lastMonths") if unit.startswith(("месяц", "month"))
            else (7, "lastWeeks") if unit.startswith(("недел", "week"))
            else (1, "lastDays")
        )
        appends.append(f"modified:<{count * factor}d")
        chips.append(Chip(kind, str(count), f"modified:<{count * factor}d", span.group(0)))
        text = text[:span.start()] + " " + text[span.end():]
    return text


def compile_intent(raw: str, now: float | None = None) -> tuple[str, list[Chip]]:
    """Turn ordinary phrasing into grammar. Returns the rewritten query and chips."""
    now = time.time() if now is None else now
    if _has_explicit_filter(raw):
        return raw, []

    work = raw
    appends: list[str] = []
    chips: list[Chip] = []

    work = _compile_time_travel(work, now, appends, chips)

    for pattern, order, value in _SORT_RULES:
        match = re.search(pattern, work, re.IGNORECASE)
        if match:
            work = work[:match.start()] + " " + work[match.end():]
            appends.append(f"sort:{order}")
            chips.append(Chip("sort", value, f"sort:{order}", match.group(0).strip()))
            break

    for pattern, spec, kind in _TIME_RULES:
        match = re.search(pattern, work, re.IGNORECASE)
        if match:
            work = work[:match.start()] + " " + work[match.end():]
            appends.append(spec)
            chips.append(Chip(kind, "", spec, match.group(0).strip()))

    for pattern, sign, kind in _SIZE_RULES:
        match = re.search(pattern, work, re.IGNORECASE)
        if not match:
            continue
        work = work[:match.start()] + " " + work[match.end():]
        if sign in ("<", ">"):
            number = (match.group(1) or match.group(3) or "").replace(",", ".")
            unit = (match.group(2) or match.group(4) or "mb").lower()
            spec = f"size:{sign}{number}{unit}"
            value = f"{number} {unit}"
        else:
            spec = f"size:{sign}"
            value = ""
        appends.append(spec)
        chips.append(Chip(kind, value, spec, match.group(0).strip()))
        break

    for pattern, kind, value in _TYPE_RULES:
        match = re.search(pattern, work, re.IGNORECASE)
        if not match:
            continue
        work = work[:match.start()] + " " + work[match.end():]
        spec = f"type:{kind}"
        appends.append(spec)
        chips.append(Chip("type", value, spec, match.group(0).strip()))

    # «самые большие файлы» is a sort, not a search for the word «файлы». Once
    # something has been compiled, a leftover generic noun carries no meaning.
    if chips:
        work = re.sub(r"(?i)\b(?:файл\w*|files?)\b", " ", work)

    rewritten = " ".join(part for part in [work.strip(), " ".join(appends)] if part)
    return rewritten, chips


def _has_explicit_filter(raw: str) -> bool:
    for token in tokenize(raw):
        if token.kind == "key" or token.kind == "regex":
            return True
    return False


def understand(raw: str, now: float | None = None) -> ParsedQuery:
    """Intent first, then grammar. The single entry point the callers use."""
    rewritten, chips = compile_intent(raw, now)
    query = parse(rewritten, now)
    query.raw = raw
    # The compiled filters are parsed again below, so drop the duplicate chips
    # they produce: the intent chip already says the same thing, with the words
    # that caused it.
    compiled = {chip.raw for chip in chips}
    query.chips = chips + [chip for chip in query.chips if chip.raw not in compiled]
    return query


def cheat_sheet() -> list[tuple[str, str]]:
    """Rows for the UI help block and the README table."""
    return [
        ("type:pdf", "категория или расширение"),
        ("ext:rs", "точное расширение"),
        ('in:"C:\\Docs"', "только внутри папки"),
        ("path:src", "подстрока пути"),
        ("modified:<7d", "изменён за неделю"),
        ("size:>2mb", "размер больше 2 МБ"),
        ("lang:python", "семейство языка"),
        ("name:config", "только имя файла"),
        ("content:timeout", "только содержимое"),
        ("sym:parse_query", "определение в коде"),
        ('re:"TODO|FIXME"', "регулярное выражение"),
        ("-node_modules", "исключить"),
        ('"exact phrase"', "фраза целиком"),
        ("mode:exhaustive", "все совпадения по пути"),
        ("sort:newest", "сортировка"),
    ]
