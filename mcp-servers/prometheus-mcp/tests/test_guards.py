from __future__ import annotations

import pytest

from prometheus_mcp.config import ServerSettings
from prometheus_mcp.guards import (
    GuardError,
    check_query,
    parse_duration,
    parse_step,
    parse_time,
    validate_range,
)

S = ServerSettings(max_range_hours=6, max_points=100, min_step_s=15, max_query_length=300)


def test_durations_and_steps() -> None:
    assert parse_duration("5m") == 300
    assert parse_duration("1h30m") == 5400
    assert parse_step("1m") == 60 and parse_step(30) == 30 and parse_step("45") == 45
    assert parse_step(None) is None
    with pytest.raises(GuardError, match="invalid duration"):
        parse_duration("5 minutes")
    with pytest.raises(GuardError, match="positive"):
        parse_step(0)


def test_times() -> None:
    assert parse_time("2026-09-25T10:00:00Z", "start") == 1790330400
    assert parse_time("2026-09-25T10:00:00", "start") == 1790330400  # UTC when no zone
    assert parse_time("1790330400", "start") == 1790330400
    with pytest.raises(GuardError, match="ISO-8601"):
        parse_time("yesterday", "start")


def test_range_auto_step_fits_the_point_cap() -> None:
    w = validate_range("2026-09-25T10:00:00Z", "2026-09-25T11:00:00Z", None, S)
    assert w.step == 37 and w.points <= 100  # ceil(3600 / 99)
    short = validate_range("2026-09-25T10:00:00Z", "2026-09-25T10:10:00Z", None, S)
    assert short.step == 15  # never below min_step


@pytest.mark.parametrize(
    ("start", "end", "step", "message"),
    [
        ("2026-09-25T10:00:00Z", "2026-09-25T09:00:00Z", None, "end must be after start"),
        ("2026-09-25T00:00:00Z", "2026-09-25T10:00:00Z", None, "exceeds the maximum of 6 hours"),
        ("2026-09-25T10:00:00Z", "2026-09-25T11:00:00Z", "5s", "below the minimum of 15s"),
        ("2026-09-25T10:00:00Z", "2026-09-25T11:00:00Z", "15s", "points per series exceeds"),
    ],
)
def test_range_rejections(start: str, end: str, step: str | None, message: str) -> None:
    with pytest.raises(GuardError, match=message):
        validate_range(start, end, step, S)


def test_metric_names_are_extracted() -> None:
    q = (
        "histogram_quantile(0.95, sum by (le, service) "
        '(rate(http_request_duration_seconds_bucket{service="payment-service",le!="+Inf"}[5m])))'
    )
    assert check_query(q, S) == ["http_request_duration_seconds_bucket"]
    q2 = (
        'sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m])) '
        "> 0.05 and on (service) db_pool_connections_pending offset 1h > 1e3"
    )
    assert check_query(q2, S) == ["http_requests_total", "db_pool_connections_pending"]
    q3 = (
        "max by (namespace, pod) (kube_pod_container_status_restarts_total) * on (namespace, pod) "
        "group_left (label_app) kube_pod_labels"
    )
    assert check_query(q3, S) == ["kube_pod_container_status_restarts_total", "kube_pod_labels"]
    assert check_query("vector(1)", S) == []


@pytest.mark.parametrize(
    ("query", "message"),
    [
        ("", "must not be empty"),
        ("x" * 301, "maximum is 300"),
        ("up\x00", "control characters"),
        ('{job=~".+"}', "needs a metric name"),
        ('count({__name__=~".+"})', "needs a metric name"),
        ('count(up{__name__=~".+"})', "__name__"),
        ("rate(http_requests_total[30d])", "exceeds the maximum of 6 hours"),
        ("max_over_time(up[1h:1s])", "below the minimum step"),
        ("up offset 7d", "offset 7d exceeds"),
    ],
)
def test_pathological_queries_are_rejected(query: str, message: str) -> None:
    with pytest.raises(GuardError, match=message):
        check_query(query, S)


def test_allowlist() -> None:
    s = ServerSettings(metric_allowlist=("http_.*", "up"))
    assert check_query("sum(rate(http_requests_total[5m]))", s) == ["http_requests_total"]
    with pytest.raises(GuardError, match="not allowed by the server allowlist: secret_metric"):
        check_query("up + secret_metric", s)
    # strings are not metric names
    assert check_query('label_replace(up, "dst", "$1", "src", "(secret_metric)")', s) == ["up"]
