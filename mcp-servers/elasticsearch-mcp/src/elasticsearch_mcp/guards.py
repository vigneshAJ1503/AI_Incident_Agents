"""Server-side guardrails: index allowlist, time-range limits, ES|QL scope.

Enforced in the MCP server itself so that *any* client (not only our agents)
gets the same protection.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase

_ESQL_FROM = re.compile(r"^\s*FROM\s+(?P<indices>[^\s|]+(?:\s*,\s*[^\s|]+)*)", re.IGNORECASE)
_ESQL_FORBIDDEN = re.compile(r"\b(ENRICH|LOOKUP)\b", re.IGNORECASE)


class GuardError(ValueError):
    """A request violates a guardrail. The message is shown to the model."""


@dataclass(frozen=True)
class TimeWindow:
    start: datetime
    end: datetime

    def as_filter(self, field: str) -> dict[str, object]:
        return {
            "range": {
                field: {
                    "gte": self.start.isoformat(),
                    "lte": self.end.isoformat(),
                    "format": "strict_date_optional_time",
                }
            }
        }


def validate_index(index: str, allowed: tuple[str, ...]) -> list[str]:
    """Every comma-separated part must be covered by an allowed pattern."""
    parts = [p.strip() for p in index.split(",") if p.strip()]
    if not parts:
        raise GuardError("index is required")
    for part in parts:
        if part in ("*", "_all") or part.startswith(("-", ".")):
            raise GuardError(f"index '{part}' is not allowed")
        if not any(fnmatchcase(part, pattern) for pattern in allowed):
            raise GuardError(
                f"index '{part}' is outside the allowed patterns: {', '.join(allowed)}"
            )
    return parts


def parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise GuardError(
            f"{name} must be an ISO-8601 timestamp, e.g. 2026-09-25T10:00:00Z (got '{value}')"
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_time_range(start: str, end: str, max_hours: float) -> TimeWindow:
    window = TimeWindow(parse_time(start, "start"), parse_time(end, "end"))
    if window.end <= window.start:
        raise GuardError("end must be after start")
    if window.end - window.start > timedelta(hours=max_hours):
        raise GuardError(f"time range exceeds the maximum of {max_hours:g} hours")
    return window


def validate_esql(query: str, allowed: tuple[str, ...]) -> list[str]:
    match = _ESQL_FROM.match(query)
    if not match:
        raise GuardError("ES|QL query must start with 'FROM <index-pattern>'")
    if _ESQL_FORBIDDEN.search(query):
        raise GuardError("ENRICH and LOOKUP JOIN are not allowed")
    return validate_index(match.group("indices"), allowed)


def clamp_size(size: int, max_results: int) -> int:
    if size < 1:
        raise GuardError("size must be >= 1")
    return min(size, max_results)
