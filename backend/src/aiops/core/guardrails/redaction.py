"""Redact secrets and PII from tool output before it reaches an LLM.

JSON payloads are redacted value-by-value (strings only), so numbers such as
timestamps are never corrupted and the JSON stays valid.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable
from typing import Any

Replacer = Callable[[re.Match[str]], str]


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _tag(kind: str) -> str:
    return f"[REDACTED:{kind}]"


# kind -> list of (pattern, replacer or None for full replacement). Order matters.
_PATTERNS: dict[str, list[tuple[re.Pattern[str], Replacer | None]]] = {
    "jwt": [(re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}"), None)],
    "bearer_tokens": [
        (
            re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}"),
            lambda m: f"{m.group(1)} {_tag('bearer_tokens')}",
        )
    ],
    "api_keys": [
        (
            re.compile(
                r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"
                r"|\bgsk_[A-Za-z0-9]{20,}"
                r"|\bAKIA[0-9A-Z]{16}\b"
                r"|\bgh[pousr]_[A-Za-z0-9]{30,}"
                r"|\bxox[abpr]-[A-Za-z0-9-]{10,}"
                r"|\bAIza[0-9A-Za-z_-]{35}"
            ),
            None,
        ),
        (
            re.compile(
                r"(?i)\b(api[_-]?key|secret|password|passwd|pwd|token)(\s*[:=]\s*)['\"]?[^\s'\",;&]{4,}"
            ),
            lambda m: f"{m.group(1)}{m.group(2)}{_tag('api_keys')}",
        ),
    ],
    "emails": [(re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), None)],
    "credit_cards": [
        (
            re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
            lambda m: (
                _tag("credit_cards") if _luhn_ok(re.sub(r"\D", "", m.group(0))) else m.group(0)
            ),
        )
    ],
    "ip_addresses": [
        (re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), None)
    ],
}

SUPPORTED_KINDS = frozenset(_PATTERNS)


class Redactor:
    def __init__(self, kinds: Iterable[str]) -> None:
        kinds = list(kinds)
        unknown = set(kinds) - SUPPORTED_KINDS
        if unknown:
            raise ValueError(
                f"Unknown redaction kinds {sorted(unknown)}; supported: {sorted(SUPPORTED_KINDS)}"
            )
        # Keep canonical order so e.g. JWTs are removed before generic token patterns.
        self._kinds = [k for k in _PATTERNS if k in kinds]
        self.counts: Counter[str] = Counter()

    def text(self, value: str) -> str:
        for kind in self._kinds:
            for pattern, replacer in _PATTERNS[kind]:

                def _replace(
                    match: re.Match[str], kind: str = kind, replacer: Replacer | None = replacer
                ) -> str:
                    out = replacer(match) if replacer else _tag(kind)
                    if out != match.group(0):
                        self.counts[kind] += 1
                    return out

                value = pattern.sub(_replace, value)
        return value

    def data(self, value: Any) -> Any:
        """Recursively redact string values (and keys) of JSON-like data."""
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {self.text(str(k)): self.data(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.data(v) for v in value]
        return value
