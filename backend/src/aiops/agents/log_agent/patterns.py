"""Log message templating: turn variable messages into stable patterns.

    "... within 5000ms (pool size=2, active=2, waiting=17)"
 -> "... within <NUM>ms (pool size=<NUM>, active=<NUM>, waiting=<NUM>)"

Deterministic and dependency-free; good enough to merge the variants that
ES|QL groups separately (ids, counts, durations, hosts).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field

_MASKS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
        ),
        "<UUID>",
    ),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "<EMAIL>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\b"), "<TS>"),
    (re.compile(r"\b(?=[0-9a-fA-F]*\d)(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{12,}\b"), "<HEX>"),
    (re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?"), "<NUM>"),
]
_SPACES = re.compile(r"\s+")


def template(message: str) -> str:
    out = message
    for pattern, token in _MASKS:
        out = pattern.sub(token, out)
    return _SPACES.sub(" ", out).strip()


def like_prefix(template_text: str, min_len: int = 8) -> str | None:
    """Literal prefix before the first placeholder, for an ES|QL LIKE filter."""
    prefix = template_text.split("<", 1)[0].rstrip()
    if len(prefix) < min_len:
        return None
    return prefix


@dataclass
class Pattern:
    template: str
    level: str
    count: int = 0
    first_seen: str | None = None
    last_seen: str | None = None
    examples: list[str] = field(default_factory=list)

    def add(self, count: int, first: str | None, last: str | None, message: str) -> None:
        self.count += count
        if first and (self.first_seen is None or first < self.first_seen):
            self.first_seen = first
        if last and (self.last_seen is None or last > self.last_seen):
            self.last_seen = last
        if len(self.examples) < 2 and message not in self.examples:
            self.examples.append(message)


def cluster(rows: Iterable[tuple[int, str | None, str | None, str, str]]) -> list[Pattern]:
    """rows: (count, first_seen, last_seen, level, message) -> patterns sorted by count."""
    patterns: dict[tuple[str, str], Pattern] = {}
    for count, first, last, level, message in rows:
        key = (level, template(message))
        if key not in patterns:
            patterns[key] = Pattern(template=key[1], level=level)
        patterns[key].add(int(count), first, last, message)
    return sorted(patterns.values(), key=lambda p: (-p.count, p.template))
