"""Deep links for evidence, built from ``ui_link_template``-style settings.

Links are plain string templates in capability settings, so a company swaps Grafana
for Datadog/New Relic/its own console by config only. No UI (and no Grafana MCP) has to
run for an agent to produce them; they only need to open for a human.

Placeholders shared by the time-window helpers:
  {start} {end}        ISO-8601 UTC, e.g. 2026-09-25T10:00:00Z
  {end_utc}            2026-09-25%2010:00:00 (UTC, URL-encoded; the Prometheus UI end_input)
  {from_ms} {to_ms}    epoch milliseconds (Grafana ``from``/``to``)
  {range}              window length as a Prometheus duration, e.g. 1h30m
  {query}              URL-encoded query text (PromQL, KQL, ...)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

log = logging.getLogger(__name__)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def duration(seconds: float) -> str:
    """Prometheus duration for a window length: 5400 -> '1h30m', 45 -> '45s'."""
    total = max(round(seconds), 1)
    parts = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        count, total = divmod(total, size)
        if count:
            parts.append(f"{count}{unit}")
    return "".join(parts)


def window_values(start: datetime, end: datetime) -> dict[str, str]:
    return {
        "start": iso(start),
        "end": iso(end),
        "end_utc": quote(end.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"), safe=":"),
        "from_ms": str(int(start.timestamp() * 1000)),
        "to_ms": str(int(end.timestamp() * 1000)),
        "range": duration((end - start).total_seconds()),
    }


def format_link(template: str | None, **values: Any) -> str | None:
    """Fill a link template; None when unset or when it names an unknown placeholder
    (a bad template must never fail an investigation)."""
    if not template:
        return None
    try:
        return template.format(**values)
    except (KeyError, IndexError, ValueError) as exc:
        log.warning("Link template %r could not be filled: %s", template, exc)
        return None


def query_link(
    template: str | None, query: str, start: datetime, end: datetime, **values: Any
) -> str | None:
    """A link that opens ``query`` over [start, end] (e.g. Prometheus graph, Grafana Explore)."""
    return format_link(template, query=quote(query, safe=""), **window_values(start, end), **values)
