"""Server-side guardrails: label-filter validation, result caps, time ranges.

Enforced in the MCP server itself so that *any* client (not only our agents)
gets the same protection. Filters are equality matchers only: no regexes, so a
model can't write an expensive or overly broad matcher.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,127}$")
FINGERPRINT = re.compile(r"^[0-9a-f]{1,32}$")
MAX_LABEL_VALUE = 256

AlertState = Literal["active", "suppressed", "all"]
SilenceState = Literal["active", "pending", "expired", "all"]


class GuardError(ValueError):
    """A request violates a guardrail. The message is shown to the model."""


@dataclass(frozen=True)
class StateFlags:
    active: bool
    silenced: bool
    inhibited: bool


STATES: dict[str, StateFlags] = {
    "active": StateFlags(active=True, silenced=False, inhibited=False),
    "suppressed": StateFlags(active=False, silenced=True, inhibited=True),
    "all": StateFlags(active=True, silenced=True, inhibited=True),
}


def state_flags(state: str) -> StateFlags:
    try:
        return STATES[state]
    except KeyError:
        raise GuardError(f"state must be one of {sorted(STATES)} (got '{state}')") from None


def build_matchers(labels: dict[str, str], max_filters: int) -> list[str]:
    """Validate label filters and turn them into Alertmanager equality matchers."""
    if len(labels) > max_filters:
        raise GuardError(f"at most {max_filters} label filters are allowed (got {len(labels)})")
    matchers = []
    for name, value in sorted(labels.items()):
        if not LABEL_NAME.match(name):
            raise GuardError(
                f"invalid label name '{name}': use letters, digits and '_' (e.g. 'service')"
            )
        if not isinstance(value, str) or not value:
            raise GuardError(f"label '{name}' needs a non-empty string value")
        if len(value) > MAX_LABEL_VALUE:
            raise GuardError(f"label '{name}' value is longer than {MAX_LABEL_VALUE} characters")
        if any(ord(c) < 32 for c in value):
            raise GuardError(f"label '{name}' value contains control characters")
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        matchers.append(f'{name}="{escaped}"')
    return matchers


def merge_filters(
    labels: dict[str, str] | None,
    *,
    service: str | None = None,
    severity: str | None = None,
    alertname: str | None = None,
) -> dict[str, str]:
    """Shortcut arguments + free label filters; a conflict is an error, not a silent override."""
    merged = dict(labels or {})
    for name, value in (("service", service), ("severity", severity), ("alertname", alertname)):
        if value is None:
            continue
        if name in merged and merged[name] != value:
            raise GuardError(f"conflicting filters for '{name}': '{merged[name]}' vs '{value}'")
        merged[name] = value
    return merged


def validate_fingerprint(fingerprint: str) -> str:
    value = fingerprint.strip().lower()
    if not FINGERPRINT.match(value):
        raise GuardError(
            f"fingerprint must be a hex string like '45410293e243ba8c' (got '{fingerprint}')"
        )
    return value


def clamp_limit(limit: int, max_results: int) -> int:
    if limit < 1:
        raise GuardError("limit must be >= 1")
    return min(limit, max_results)


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


def validate_time_range(start: str, end: str, max_hours: float) -> tuple[datetime, datetime]:
    begin, finish = parse_time(start, "start"), parse_time(end, "end")
    if finish <= begin:
        raise GuardError("end must be after start")
    if finish - begin > timedelta(hours=max_hours):
        raise GuardError(f"time range exceeds the maximum of {max_hours:g} hours")
    return begin, finish
