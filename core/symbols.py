"""Code definitions, extracted without a parser.

kensaku runs tree-sitter and gets real syntax trees; this is the dependency-free
version of the same feature. A pattern table per language fires only on lines
that are *shaped* like a definition — `def name(`, `class Name`, `fn name(`,
`func (r *T) Name(` — and a few things are deliberately not attempted:

* nested definitions are recorded at their own line, not nested under a parent;
* a name that appears inside a string or a comment is skipped, because the
  patterns are anchored at the start of a line and allow only modifiers before
  the keyword;
* a file with no recognisable definitions gets no rows, which is correct: an
  empty result is a better answer than a wrong one.

The cost is that a clever definition (`foo = lambda: 1`, a macro that generates
twenty methods) is invisible. The benefit is that this runs on every code file
during indexing in about a millisecond and needs nothing installed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# One entry per language family. Order inside a table matters: the first match
# wins, so the more specific pattern is listed first.
PYTHON: list[tuple[str, re.Pattern[str]]] = [
    ("class", re.compile(r"^\s*class\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")),
]

JS: list[tuple[str, re.Pattern[str]]] = [
    ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
    ("enum", re.compile(r"^\s*(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")),
    ("type", re.compile(r"^\s*(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\s*=")),
    ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)")),
    ("function", re.compile(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*"
        r"(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
    )),
    ("method", re.compile(r"^\s{2,}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{")),
]

RUST: list[tuple[str, re.Pattern[str]]] = [
    ("struct", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?struct\s+([A-Za-z_]\w*)")),
    ("enum", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?enum\s+([A-Za-z_]\w*)")),
    ("trait", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:unsafe\s+)?trait\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+)?(?:async\s+)?(?:unsafe\s+)?(?:extern\s+\"[^\"]*\"\s+)?fn\s+([A-Za-z_]\w*)")),
    ("type", re.compile(r"^\s*(?:pub\s+)?type\s+([A-Za-z_]\w*)")),
    ("impl", re.compile(r"^\s*impl(?:<[^>]*>)?\s+(?:[\w:<>]+\s+for\s+)?([A-Za-z_]\w*)")),
    ("macro", re.compile(r"^\s*macro_rules!\s*([A-Za-z_]\w*)")),
]

GO: list[tuple[str, re.Pattern[str]]] = [
    ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(?:struct|interface)")),
    ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*[\[(]")),
]

JVM: list[tuple[str, re.Pattern[str]]] = [
    ("class", re.compile(
        r"^\s*(?:@\w+\s*)*(?:public|private|protected|internal|static|final|abstract|"
        r"sealed|partial|open|data|\s)*\b(?:class|interface|enum|record|object)\s+([A-Za-z_]\w*)"
    )),
    ("method", re.compile(
        r"^\s*(?:public|private|protected|internal|static|final|abstract|override|"
        r"virtual|async|synchronized|native|\s)+[\w<>\[\],.?\s]+?\s+([A-Za-z_]\w*)\s*\("
    )),
]

RUBY: list[tuple[str, re.Pattern[str]]] = [
    ("class", re.compile(r"^\s*(?:class|module)\s+([A-Z]\w*)")),
    ("method", re.compile(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[?!=]?)")),
]

PHP: list[tuple[str, re.Pattern[str]]] = [
    ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?(?:class|interface|trait|enum)\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:public|private|protected|static|final|\s)*function\s+&?([A-Za-z_]\w*)")),
]

SHELL: list[tuple[str, re.Pattern[str]]] = [
    ("function", re.compile(r"^\s*(?:function\s+)?([A-Za-z_][\w-]*)\s*\(\)\s*\{")),
]

SQL: list[tuple[str, re.Pattern[str]]] = [
    ("table", re.compile(r"^\s*create\s+(?:or\s+replace\s+)?(?:temp\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.\"]+)", re.I)),
    ("function", re.compile(r"^\s*create\s+(?:or\s+replace\s+)?function\s+([\w.\"]+)", re.I)),
]

LANGUAGES: dict[str, list[tuple[str, re.Pattern[str]]]] = {
    "python": PYTHON,
    "js": JS,
    "rust": RUST,
    "go": GO,
    "jvm": JVM,
    "ruby": RUBY,
    "php": PHP,
    "shell": SHELL,
    "sql": SQL,
}

EXTENSIONS: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".mjs": "js", ".cjs": "js", ".jsx": "js",
    ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js", ".vue": "js", ".svelte": "js",
    ".rs": "rust",
    ".go": "go",
    ".java": "jvm", ".kt": "jvm", ".kts": "jvm", ".scala": "jvm",
    ".cs": "jvm", ".groovy": "jvm",
    ".rb": "ruby",
    ".php": "php",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "shell",
    ".sql": "sql",
}

# A file that matches more often than this is generated — a lockfile, a bundle,
# a migration dump — and its "definitions" are noise that would bury the real
# ones. Cheaper to drop the file than to rank around it.
MAX_SYMBOLS = 4000


@dataclass(frozen=True, slots=True)
class Symbol:
    name: str
    kind: str
    line: int
    text: str

    def as_tuple(self) -> tuple[str, str, int, str]:
        return (self.name, self.kind, self.line, self.text)


def language_for(path: str | Path) -> str | None:
    suffix = Path(str(path)).suffix.lower()
    return EXTENSIONS.get(suffix)


def extract(text: str, language: str, limit: int = MAX_SYMBOLS) -> list[Symbol]:
    """Definitions in `text`, in source order. One line of context each."""
    patterns = LANGUAGES.get(language)
    if not patterns or not text:
        return []
    found: list[Symbol] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "*", "/*")):
            continue
        for kind, pattern in patterns:
            match = pattern.match(line)
            if match:
                name = match.group(1).strip("\"'`[]")
                if not name or len(name) > 120:
                    break
                found.append(Symbol(name, kind, number, stripped[:200]))
                if len(found) >= limit:
                    return found
                break
    return found


def extract_for(path: str | Path, text: str) -> list[Symbol]:
    """Convenience: pick the language from the extension, then extract."""
    language = language_for(path)
    return extract(text, language) if language else []
