"""`.gitignore` / `.cursorignore` / `.semfindignore` parity for the index.

If a repository ignores a file, the index should not surface it: otherwise the
search shows build output and secrets the project already decided to hide. This
is a matching-parity subset, not a full gitignore engine — `*`, `?`, `**`,
anchored patterns, directory-only patterns, negation and comments are supported;
escaped trailing spaces are not.

Precedence follows git: once a parent directory is excluded nothing under it can
be re-included, the deepest applicable rule wins, later lines win inside a file,
and `!` negates.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

IGNORE_FILES = (".gitignore", ".cursorignore", ".semfindignore")
MAX_IGNORE_FILES = 2000


def _glob_to_regex(pattern: str) -> re.Pattern:
    out: list[str] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "*":
            if pattern[index:index + 2] == "**":
                out.append(".*")
                index += 2
                if index < len(pattern) and pattern[index] == "/":
                    index += 1
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char in ".+()[]{}|^$\\":
            out.append("\\" + char)
        else:
            out.append(char)
        index += 1
    return re.compile("".join(out) + "$")


class Rule:
    __slots__ = ("pattern", "negated", "dir_only", "anchored", "regex")

    def __init__(self, pattern: str, negated: bool, dir_only: bool, anchored: bool) -> None:
        self.pattern = pattern
        self.negated = negated
        self.dir_only = dir_only
        self.anchored = anchored
        self.regex = _glob_to_regex(pattern)

    def matches(self, relative: str, is_dir: bool) -> bool:
        if self.dir_only and not is_dir:
            return False
        target = relative if self.anchored else relative.rsplit("/", 1)[-1]
        return bool(self.regex.match(target))


def parse(text: str) -> list[Rule]:
    rules: list[Rule] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:].strip()
        if not line:
            continue
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        line = line.lstrip("/")
        if line:
            rules.append(Rule(line, negated, dir_only, anchored))
    return rules


class IgnoreSet:
    """Rules grouped by the directory that declared them."""

    def __init__(self, case_insensitive: bool | None = None) -> None:
        self.rules: dict[str, list[Rule]] = {}
        self.case_insensitive = (
            os.name == "nt" if case_insensitive is None else case_insensitive
        )

    def __bool__(self) -> bool:
        return bool(self.rules)

    def add_directory(self, directory: str | Path) -> bool:
        """Load ignore files that live in `directory`. True when one was found."""
        base = str(directory)
        found = False
        for name in IGNORE_FILES:
            candidate = Path(base) / name
            if not candidate.is_file():
                continue
            try:
                text = candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rules = parse(text)
            if rules:
                self.rules.setdefault(self._key(base), []).extend(rules)
                found = True
            if len(self.rules) > MAX_IGNORE_FILES:
                break
        return found

    def _key(self, path: str) -> str:
        normalised = path.replace("\\", "/").rstrip("/")
        return normalised.lower() if self.case_insensitive else normalised

    def is_ignored(self, path: str, is_dir: bool) -> bool:
        if not self.rules:
            return False
        target = self._key(str(path))
        applicable = [
            (base, rules) for base, rules in self.rules.items()
            if target == base or target.startswith(base + "/")
        ]
        if not applicable:
            return False
        applicable.sort(key=lambda item: len(item[0]))

        ignored = False
        for base, rules in applicable:
            relative = target[len(base):].lstrip("/")
            if not relative:
                continue
            parts = relative.split("/")
            for depth in range(1, len(parts)):
                if self._evaluate(rules, "/".join(parts[:depth]), True):
                    return True  # a pruned parent cannot be re-included
            verdict = self._evaluate(rules, relative, is_dir)
            if verdict is not None:
                ignored = verdict
        return ignored

    @staticmethod
    def _evaluate(rules: list[Rule], relative: str, is_dir: bool) -> bool | None:
        verdict: bool | None = None
        for rule in rules:
            if rule.matches(relative, is_dir):
                verdict = not rule.negated
        return verdict


def load_for(paths: list[str | Path]) -> IgnoreSet:
    """Convenience for callers that already know the directories to inspect."""
    ignore = IgnoreSet()
    for directory in paths:
        ignore.add_directory(directory)
    return ignore
