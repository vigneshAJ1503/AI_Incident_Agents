"""Server-side query safety: every check runs before Prometheus is contacted.

PromQL isn't parsed fully here; a small lexer is enough to enforce what matters:
  * bounded time: query_range spans, [range] selectors, subqueries and offsets stay within
    ``max_range_hours``; steps never go below ``min_step_s`` or above ``max_points`` per series;
  * every selector names its metric (no bare ``{job=~".+"}`` or ``__name__`` matchers that
    scan the whole TSDB), and names can be restricted by an allowlist;
  * a size limit and no control characters.
Prometheus itself still enforces ``timeout`` and ``--query.max-samples``.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from prometheus_mcp.config import ServerSettings


class GuardError(ValueError):
    pass


_DURATION_PART = re.compile(r"(\d+)(ms|s|m|h|d|w|y)")
_DURATION = re.compile(r"^(?:\d+(?:ms|s|m|h|d|w|y))+$")
_UNIT_S = {"ms": 0.001, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "y": 31536000}
_STRINGS = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|`[^`]*`')
_BRACKETS = re.compile(r"\[([^\]]*)\]")
_OFFSET = re.compile(r"\boffset\s+(-?)\s*([0-9a-z]+)", re.IGNORECASE)
_AT = re.compile(r"@\s*(?:start\(\s*\)|end\(\s*\)|[0-9.eE+-]+)")
_GROUPING = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^)]*\)")
_NUMBER = re.compile(r"(?<![\w:])(?:0x[0-9a-fA-F]+|\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+)")
_IDENT = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_KEYWORDS = frozenset(
    {"and", "or", "unless", "bool", "offset", "inf", "nan", "by", "without", "on", "ignoring"}
    | {"group_left", "group_right", "start", "end"}
)


def parse_duration(text: str) -> float:
    """Prometheus duration ('5m', '1h30m', '90s') -> seconds."""
    value = text.strip()
    if not _DURATION.match(value):
        raise GuardError(f"invalid duration '{text}' (use e.g. 30s, 5m, 1h)")
    return sum(int(n) * _UNIT_S[u] for n, u in _DURATION_PART.findall(value))


def parse_time(value: str | float | int, name: str) -> float:
    """ISO-8601 (UTC if no zone) or unix seconds -> unix seconds."""
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise GuardError(
            f"{name} must be ISO-8601 (e.g. 2026-09-25T10:00:00Z) or unix seconds, got '{value}'"
        ) from None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).timestamp()


def parse_step(step: str | int | float | None) -> int | None:
    if step is None or step == "":
        return None
    if isinstance(step, int | float):
        seconds = float(step)
    else:
        text = step.strip()
        try:
            seconds = float(text)
        except ValueError:
            seconds = parse_duration(text)
    if seconds <= 0:
        raise GuardError("step must be positive")
    return math.ceil(seconds)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class RangeWindow:
    start: float
    end: float
    step: int

    @property
    def points(self) -> int:
        return int((self.end - self.start) // self.step) + 1


def validate_range(
    start: str | float, end: str | float, step: str | int | float | None, s: ServerSettings
) -> RangeWindow:
    begin, finish = parse_time(start, "start"), parse_time(end, "end")
    if finish <= begin:
        raise GuardError("end must be after start")
    span = finish - begin
    if span > s.max_range_hours * 3600:
        raise GuardError(
            f"time range {span / 3600:.1f}h exceeds the maximum of {s.max_range_hours:g} hours"
        )
    wanted = parse_step(step)
    if wanted is None:  # auto: the finest step that fits max_points
        wanted = max(s.min_step_s, math.ceil(span / (s.max_points - 1)))
    if wanted < s.min_step_s:
        raise GuardError(f"step {wanted}s is below the minimum of {s.min_step_s}s")
    window = RangeWindow(begin, finish, wanted)
    if window.points > s.max_points:
        raise GuardError(
            f"{window.points} points per series exceeds the maximum of {s.max_points}: "
            f"use step >= {math.ceil(span / (s.max_points - 1))}s or a shorter range"
        )
    return window


def validate_limit(limit: int, cap: int) -> int:
    if limit < 1:
        raise GuardError("limit must be >= 1")
    return min(limit, cap)


def validate_metric_name(name: str) -> str:
    if not _METRIC_NAME.match(name):
        raise GuardError(f"invalid metric name '{name}'")
    return name


def metric_allowed(name: str, allowlist: tuple[str, ...]) -> bool:
    return not allowlist or any(re.fullmatch(p, name) for p in allowlist)


def check_query(query: str, s: ServerSettings) -> list[str]:
    """Validate a PromQL expression; returns the metric names it selects."""
    if not query or not query.strip():
        raise GuardError("query must not be empty")
    if len(query) > s.max_query_length:
        raise GuardError(f"query is {len(query)} characters; the maximum is {s.max_query_length}")
    if _CONTROL.search(query):
        raise GuardError("query contains control characters")

    text = _STRINGS.sub('""', query)
    max_s = s.max_range_hours * 3600

    for inner in _BRACKETS.findall(text):  # [5m] range selectors and [1h:1m] subqueries
        rng, _, resolution = inner.partition(":")
        if parse_duration(rng) > max_s:
            raise GuardError(f"range [{inner}] exceeds the maximum of {s.max_range_hours:g} hours")
        if resolution.strip() and parse_duration(resolution) < s.min_step_s:
            raise GuardError(
                f"subquery resolution [{inner}] is below the minimum step of {s.min_step_s}s"
            )
    for _sign, duration in _OFFSET.findall(text):
        if parse_duration(duration) > max_s:
            raise GuardError(
                f"offset {duration} exceeds the maximum of {s.max_range_hours:g} hours"
            )

    # Every {...} label matcher must follow a metric name: no whole-TSDB scans.
    for match in re.finditer(r"\{", text):
        before = text[: match.start()].rstrip()
        if not before or not (before[-1].isalnum() or before[-1] in "_:"):
            raise GuardError(
                'every selector needs a metric name, e.g. http_requests_total{service="x"} '
                "(bare {…} selectors are not allowed)"
            )
    if re.search(r"\b__name__\b", text):
        raise GuardError("matching on __name__ is not allowed; name the metric instead")

    stripped = re.sub(r"\{[^}]*\}", " ", text)
    stripped = _BRACKETS.sub(" ", stripped)
    stripped = _OFFSET.sub(" ", stripped)
    stripped = _AT.sub(" ", stripped)
    stripped = _GROUPING.sub(" ", stripped)
    stripped = _NUMBER.sub(" ", stripped)
    names: list[str] = []
    for m in _IDENT.finditer(stripped):
        word = m.group(0)
        if word.lower() in _KEYWORDS:
            continue
        if re.match(r"\s*\(", stripped[m.end() :]):  # a function call
            continue
        if word not in names:
            names.append(word)
    blocked = [n for n in names if not metric_allowed(n, s.metric_allowlist)]
    if blocked:
        raise GuardError(f"metric(s) not allowed by the server allowlist: {', '.join(blocked)}")
    return names
