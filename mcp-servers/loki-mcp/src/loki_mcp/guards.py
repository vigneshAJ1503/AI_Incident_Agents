"""Server-side query safety: every check runs before Loki is contacted.

LogQL isn't parsed fully here; a small scanner (aware of "..." and `...` strings, so
regexes and line_format templates can't hide braces) is enough to enforce what matters:
  * every stream selector ``{...}`` carries an allowlisted exact matcher
    (e.g. ``namespace="prod"``): no queries over other namespaces/tenants' streams;
  * bounded time: the query_range span, every ``[range]`` and every ``offset`` stay within
    ``max_range_hours``; line limits, series and points per series are capped;
  * a size limit and no control characters.
Loki itself still enforces its ``limits_config`` (query length, series, timeouts).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from loki_mcp.config import ServerSettings


class GuardError(ValueError):
    pass


_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(ns|us|µs|ms|s|m|h|d|w|y)")
_DURATION = re.compile(r"^(?:\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h|d|w|y))+$")
_UNIT_S = {
    "ns": 1e-9,
    "us": 1e-6,
    "µs": 1e-6,
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 31536000,
}
_OFFSET = re.compile(r"\boffset\s+(-?)\s*([0-9a-zµ.]+)", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_MATCHER = re.compile(
    r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*("(?:\\.|[^"\\])*"|`[^`]*`)\s*(?:,|$)'
)


def parse_duration(text: str) -> float:
    """LogQL/Prometheus duration ('5m', '1h30m', '1531s') -> seconds."""
    value = text.strip()
    if not _DURATION.match(value):
        raise GuardError(f"invalid duration '{text}' (use e.g. 30s, 5m, 1h)")
    return sum(float(n) * _UNIT_S[u] for n, u in _DURATION_PART.findall(value))


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


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def iso_ns(ns: int) -> str:
    """Loki nanosecond timestamps -> ISO-8601 with milliseconds (like Elasticsearch)."""
    seconds, rest = divmod(ns, 1_000_000_000)
    base = datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{rest // 1_000_000:03d}Z"


def unquote(literal: str) -> str:
    if literal.startswith("`"):
        return literal[1:-1]
    try:
        value = json.loads(literal)
    except ValueError:
        raise GuardError(f"invalid string literal {literal}") from None
    return str(value)


@dataclass(frozen=True)
class Matcher:
    label: str
    op: str
    value: str


@dataclass(frozen=True)
class Scan:
    selectors: list[list[Matcher]]
    ranges: list[str]
    stripped: str  # the query with every string literal replaced by ""


def scan(query: str) -> Scan:
    """Find stream selectors and [range]s outside string literals."""
    selectors: list[list[Matcher]] = []
    ranges: list[str] = []
    out: list[str] = []
    i, n = 0, len(query)
    while i < n:
        ch = query[i]
        if ch in '"`':
            j = _string_end(query, i)
            out.append('""')
            i = j
            continue
        if ch == "{":
            j = i + 1
            while j < n and query[j] != "}":
                if query[j] in '"`':
                    j = _string_end(query, j)
                else:
                    j += 1
            if j >= n:
                raise GuardError("unbalanced '{' in query")
            selectors.append(parse_selector(query[i + 1 : j]))
            out.append("{}")
            i = j + 1
            continue
        if ch == "[":
            j = query.find("]", i)
            if j < 0:
                raise GuardError("unbalanced '[' in query")
            ranges.append(query[i + 1 : j])
            out.append("[]")
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return Scan(selectors, ranges, "".join(out))


def _string_end(text: str, start: int) -> int:
    quote = text[start]
    j = start + 1
    while j < len(text):
        if quote == '"' and text[j] == "\\":
            j += 2
            continue
        if text[j] == quote:
            return j + 1
        j += 1
    raise GuardError("unterminated string literal in query")


def parse_selector(body: str) -> list[Matcher]:
    matchers: list[Matcher] = []
    pos = 0
    body = body.strip()
    while pos < len(body):
        m = _MATCHER.match(body, pos)
        if not m or m.end() == pos:
            raise GuardError(f"invalid stream selector {{{body}}}")
        matchers.append(Matcher(m.group(1), m.group(2), unquote(m.group(3))))
        pos = m.end()
    if not matchers:
        raise GuardError("empty stream selector {} is not allowed")
    return matchers


def is_log_query(query: str) -> bool:
    """A log query starts with its stream selector; anything else is a metric query."""
    return query.lstrip().startswith("{")


def check_query(query: str, s: ServerSettings) -> Scan:
    """Validate a LogQL query against the server's guardrails."""
    if not query or not query.strip():
        raise GuardError("query must not be empty")
    if len(query) > s.max_query_length:
        raise GuardError(f"query is {len(query)} characters; the maximum is {s.max_query_length}")
    if _CONTROL.search(query):
        raise GuardError("query contains control characters")
    result = scan(query)
    if not result.selectors:
        raise GuardError('the query needs a stream selector, e.g. {namespace="prod"}')
    allowed = set(s.allowed_streams)
    if allowed:
        names = ", ".join(f'{k}="{v}"' for k, v in s.allowed_streams)
        for selector in result.selectors:
            if not any(m.op == "=" and (m.label, m.value) in allowed for m in selector):
                raise GuardError(f"every stream selector must include one of: {names}")
    max_s = s.max_range_hours * 3600
    for rng in result.ranges:
        if parse_duration(rng) > max_s:
            raise GuardError(f"range [{rng}] exceeds the maximum of {s.max_range_hours:g} hours")
    for _sign, duration in _OFFSET.findall(result.stripped):
        if parse_duration(duration) > max_s:
            raise GuardError(
                f"offset {duration} exceeds the maximum of {s.max_range_hours:g} hours"
            )
    return result


def check_label(name: str) -> str:
    if not _LABEL.match(name):
        raise GuardError(f"invalid label name '{name}'")
    return name


def default_selector(s: ServerSettings) -> str | None:
    """The allowlisted streams as one selector (label listings stay inside the allowlist)."""
    if not s.allowed_streams:
        return None
    label, value = s.allowed_streams[0]
    return "{" + f"{label}={json.dumps(value)}" + "}"


def validate_window(start: str | float, end: str | float, s: ServerSettings) -> tuple[float, float]:
    begin, finish = parse_time(start, "start"), parse_time(end, "end")
    if finish <= begin:
        raise GuardError("end must be after start")
    span = finish - begin
    if span > s.max_range_hours * 3600:
        raise GuardError(
            f"time range {span / 3600:.1f}h exceeds the maximum of {s.max_range_hours:g} hours"
        )
    return begin, finish


def validate_step(
    begin: float, finish: float, step: str | int | float | None, s: ServerSettings
) -> int:
    """Metric query_range step: auto (finest within max_points) or checked."""
    span = finish - begin
    if step is None or step == "":
        return max(s.min_step_s, math.ceil(span / (s.max_points - 1)))
    if isinstance(step, int | float):
        seconds = float(step)
    else:
        try:
            seconds = float(step)
        except ValueError:
            seconds = parse_duration(step)
    wanted = math.ceil(seconds)
    if wanted < s.min_step_s:
        raise GuardError(f"step {wanted}s is below the minimum of {s.min_step_s}s")
    points = int(span // wanted) + 1
    if points > s.max_points:
        raise GuardError(
            f"{points} points per series exceeds the maximum of {s.max_points}: "
            f"use step >= {math.ceil(span / (s.max_points - 1))}s or a shorter range"
        )
    return wanted


def validate_limit(limit: int, cap: int) -> int:
    if limit < 1:
        raise GuardError("limit must be >= 1")
    return min(limit, cap)
