"""Server-side guardrails: namespace allowlist, name/selector validation, caps, time ranges.

Enforced in the MCP server itself so that *any* client (not only our agents) gets the
same protection, on top of the read-only RBAC of the ServiceAccount.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

#: RFC 1123 subdomain (pod, deployment, service, namespace names).
NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")
#: Label selector: equality / set-based syntax only, no exotic characters.
SELECTOR = re.compile(r"^[A-Za-z0-9_.\-/=!,() ]{1,512}$")
MAX_SELECTOR_TERMS = 10
DURATION = re.compile(r"^(\d+)([smhd])$")
EVENT_TYPES = ("Warning", "Normal", "all")


class GuardError(ValueError):
    """A request violates a guardrail. The message is shown to the model."""


def check_namespace(namespace: str, allowed: tuple[str, ...]) -> str:
    value = namespace.strip()
    if value not in allowed:
        raise GuardError(
            f"namespace '{namespace}' is not allowed; allowed namespaces: {', '.join(allowed)}"
        )
    return value


def check_name(name: str, what: str = "name") -> str:
    value = name.strip()
    if not NAME.match(value):
        raise GuardError(
            f"invalid {what} '{name}': use a Kubernetes object name like 'payment-service'"
        )
    return value


def check_selector(selector: str | None) -> str | None:
    if selector is None or not selector.strip():
        return None
    value = selector.strip()
    if not SELECTOR.match(value):
        raise GuardError(
            f"invalid label_selector '{selector}': use e.g. 'app=payment-service' or "
            "'app in (redis,postgres)'"
        )
    terms = re.sub(r"\([^)]*\)", "", value).split(",")
    if len(terms) > MAX_SELECTOR_TERMS:
        raise GuardError(f"label_selector has more than {MAX_SELECTOR_TERMS} terms")
    return value


def clamp_limit(limit: int, maximum: int) -> int:
    if limit < 1:
        raise GuardError("limit must be >= 1")
    return min(limit, maximum)


def check_event_type(event_type: str) -> str:
    if event_type not in EVENT_TYPES:
        raise GuardError(f"type must be one of {', '.join(EVENT_TYPES)} (got '{event_type}')")
    return event_type


def parse_since(
    value: str | None, max_hours: float, now: datetime | None = None
) -> datetime | None:
    """'30m' / '2h' / '1d' or an ISO-8601 timestamp -> an aware UTC datetime (None = no bound)."""
    if value is None or not value.strip():
        return None
    now = now or datetime.now(UTC)
    raw = value.strip()
    match = DURATION.match(raw)
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        since = now - timedelta(seconds=seconds)
    else:
        try:
            since = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            raise GuardError(
                f"since must be a duration like '30m' / '2h' or an ISO-8601 timestamp "
                f"like 2026-09-25T10:00:00Z (got '{value}')"
            ) from None
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        since = since.astimezone(UTC)
    if now - since > timedelta(hours=max_hours):
        raise GuardError(f"since is more than {max_hours:g} hours ago (the server's maximum)")
    return since


def log_since_seconds(value: str | None, max_hours: float, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    since = parse_since(value, max_hours, now)
    if since is None:
        return int(max_hours * 3600)
    return max(1, int((now - since).total_seconds()))
