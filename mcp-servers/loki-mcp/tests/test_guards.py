from __future__ import annotations

import pytest

from loki_mcp.config import ServerSettings, parse_streams
from loki_mcp.guards import (
    GuardError,
    check_query,
    default_selector,
    is_log_query,
    iso_ns,
    parse_duration,
    parse_time,
    scan,
    validate_step,
    validate_window,
)

S = ServerSettings(max_range_hours=6, max_query_length=600)

WINDOW = (
    'label_format window=`{{ if ge (unixEpochMillis __timestamp__) "1790330400000" }}'
    "current{{ else }}baseline{{ end }}`"
)


def test_durations_and_times() -> None:
    assert parse_duration("1h30m") == 5400 and parse_duration("1531s") == 1531
    assert parse_duration("500ms") == 0.5
    with pytest.raises(GuardError):
        parse_duration("5 minutes")
    assert parse_time("2026-09-25T10:00:00Z", "t") == 1790330400
    assert parse_time("2026-09-25T10:00:00", "t") == 1790330400  # naive = UTC
    with pytest.raises(GuardError, match="ISO-8601"):
        parse_time("noon", "t")
    assert iso_ns(1790330400123456789) == "2026-09-25T10:00:00.123Z"


def test_the_agents_queries_pass() -> None:
    queries = [
        f'sum by (window, level) (count_over_time({{namespace="prod", app="payment-service"}} | {WINDOW} [1531s]))',
        (
            'label_replace(topk(1000, sum by (window, level, msg) (count_over_time({namespace="prod", '
            'app="p", level=~"^(?:ERROR|WARN)$"} | json msg="message" | drop __error__, __error_details__ '
            f'| {WINDOW} [1531s]))), "stat", "count", "", "")'
        ),
        '{namespace="prod", app="p"} |= "Database {x}" | json msg="message" | msg=~"^Database \\\\{x\\\\}"',
    ]
    for query in queries:
        check_query(query, S)


def test_braces_inside_strings_are_not_selectors() -> None:
    result = scan('{namespace="prod"} |~ "{a}[5m]" | line_format `{{.msg}} [7d]`')
    assert len(result.selectors) == 1 and result.ranges == []
    assert result.selectors[0][0].value == "prod"


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("", "must not be empty"),
        ('sum(count_over_time({app="x"} [5m]))', 'must include one of: namespace="prod"'),
        ('{namespace=~"prod|kube-system"}', "must include one of"),
        ('{namespace!="prod"}', "must include one of"),
        ('{namespace="prod"} or {namespace="staging"}', "must include one of"),
        ('count_over_time({namespace="prod"} [7d])', "exceeds the maximum of 6 hours"),
        ('count_over_time({namespace="prod"} [5m] offset 1d)', "offset 1d exceeds"),
        ("{}", "empty stream selector"),
        ('{namespace="prod"', "unbalanced"),
        ('{namespace="prod", app}', "invalid stream selector"),
        ('{namespace="prod"} |= "x\x00"', "control characters"),
        ("sum(rate(x[5m]))", "needs a stream selector"),
        ('{namespace="prod"} |= "' + "a" * 600 + '"', "the maximum is 600"),
    ],
)
def test_rejected(query: str, message: str) -> None:
    with pytest.raises(GuardError, match=message.replace("(", r"\(")):
        check_query(query, S)


def test_empty_allowlist_accepts_any_selector() -> None:
    check_query('{app=~".+"}', ServerSettings(allowed_streams=()))
    assert default_selector(ServerSettings(allowed_streams=())) is None
    assert default_selector(S) == '{namespace="prod"}'


def test_parse_streams() -> None:
    assert parse_streams(' namespace=prod, cluster="eu" ,') == (
        ("namespace", "prod"),
        ("cluster", "eu"),
    )
    with pytest.raises(ValueError, match="label=value"):
        parse_streams("prod")


def test_windows_and_steps() -> None:
    begin, end = validate_window("2026-09-25T10:00:00Z", "2026-09-25T11:00:00Z", S)
    assert end - begin == 3600
    with pytest.raises(GuardError, match="exceeds the maximum of 6 hours"):
        validate_window("2026-09-25T00:00:00Z", "2026-09-25T10:00:00Z", S)
    with pytest.raises(GuardError, match="after start"):
        validate_window("2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z", S)
    assert validate_step(begin, end, None, S) == 4  # ceil(3600 / 1099)
    assert validate_step(begin, end, "1m", S) == 60
    with pytest.raises(GuardError, match="points per series"):
        validate_step(begin, end, "1s", S)


def test_is_log_query() -> None:
    assert is_log_query('  {namespace="prod"} |= "x"')
    assert not is_log_query('count_over_time({namespace="prod"} [5m])')
