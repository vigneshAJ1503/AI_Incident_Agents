"""Server-side input validation and caps, enforced for every client."""

from __future__ import annotations

import re

_SLUG = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_DOC_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,255}\.md$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class GuardError(ValueError):
    """A request violates a guardrail. The message is shown to the model."""


def validate_query(query: str, max_chars: int) -> str:
    cleaned = _CONTROL.sub(" ", query).strip()
    if not cleaned:
        raise GuardError("query is required")
    if len(cleaned) > max_chars:
        raise GuardError(f"query is too long ({len(cleaned)} chars, max {max_chars})")
    return cleaned


def validate_k(k: int, max_k: int) -> int:
    if not 1 <= k <= max_k:
        raise GuardError(f"k must be between 1 and {max_k}")
    return k


def validate_slugs(values: list[str] | None, name: str, max_items: int) -> list[str]:
    """Filters like services/tags: lowercase slugs, bounded count."""
    if not values:
        return []
    if len(values) > max_items:
        raise GuardError(f"{name} accepts at most {max_items} values")
    cleaned = [v.strip().lower() for v in values]
    bad = [v for v in cleaned if not _SLUG.match(v)]
    if bad:
        raise GuardError(f"invalid {name} value(s): {bad} (expected e.g. 'payment-service')")
    return sorted(set(cleaned))


def validate_doc_path(path: str) -> str:
    """Repo-relative markdown path, e.g. 'knowledge-base/runbooks/redis-outage.md'."""
    cleaned = path.strip()
    if ".." in cleaned.split("/") or not _DOC_PATH.match(cleaned) or "//" in cleaned:
        raise GuardError(
            f"invalid document path '{path}' (expected e.g. 'knowledge-base/runbooks/redis-outage.md')"
        )
    return cleaned
