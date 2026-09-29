"""Deterministic metric anomaly detection (no MCP, no LLM). PromQL: tests/unit/test_providers.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aiops.agents.metrics_agent.analysis import (
    MetricsAnalysis,
    Rule,
    absolute_thresholds,
    detect,
    downsample,
    fmt,
    oom_stats,
    restarts_stats,
)
from aiops.core.config import ConfigError
from aiops.core.models import TimeRange

T0 = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
STEP = 30
WINDOW = TimeRange(start=T0 + timedelta(minutes=15), end=T0 + timedelta(minutes=30))


def series(values: list[float | None]) -> list[tuple[float, float | None]]:
    return [(T0.timestamp() + i * STEP, v) for i, v in enumerate(values)]


def flat_then(base: float, after: float, change_at: int = 40, n: int = 61) -> list[float | None]:
    """30 baseline points, then the window; the value changes at index ``change_at``."""
    return [base if i < change_at else after for i in range(n)]


def test_rule_semantics() -> None:
    up = Rule("up", min_delta=0.01, min_ratio=3.0)
    assert up.anomalous(0.05, 0.0) and not up.anomalous(0.005, 0.0)
    assert not up.anomalous(0.2, 0.1)  # only x2
    down = Rule("down", min_delta=0.2, min_ratio=0.5, min_baseline=0.2)
    assert down.anomalous(0.5, 3.0) and not down.anomalous(2.0, 3.0)
    assert not down.anomalous(0.0, 0.1)  # baseline too small to judge a drop
    assert not down.anomalous(0.0, None)
    level = Rule("up", min_delta=0.2, level=0.9)
    assert level.anomalous(1.0, 0.1) and not level.anomalous(0.8, 0.1)
    assert not level.anomalous(1.0, 0.95)  # already saturated in the baseline


def test_error_rate_change_point_and_magnitude() -> None:
    stats = detect("error_rate", "payment-service", "ratio", series(flat_then(0.0, 0.3)), WINDOW)
    a = stats.anomaly
    assert a is not None and a.direction == "up"
    assert a.start_time == T0 + timedelta(seconds=40 * STEP)  # 10:20:00
    assert a.baseline == 0.0 and a.current == pytest.approx(0.3) and a.ratio is None
    assert a.ongoing
    assert "error rate up: 0.0% -> 30.0%" in a.describe()


def test_no_baseline_without_absolute_threshold_is_never_an_anomaly() -> None:
    """A series that only starts inside the window (fresh Prometheus, new service) has
    nothing to compare with: without an absolute threshold it is noted, never flagged."""
    fresh = [None] * 30 + [9.5] * 31
    stats = detect("latency_p95", "payment-service", "seconds", series(fresh), WINDOW)
    assert stats.anomaly is None and stats.baseline is None and stats.current == 9.5
    assert stats.note and "no baseline" in stats.note
    rps = detect("rps", "payment-service", "rps", series(fresh), WINDOW, DEFAULTS)
    assert rps.anomaly is None  # traffic has no absolute threshold


DEFAULTS = absolute_thresholds(None)
FRESH = [None] * 30  # nothing before the window: a cluster that just started


@pytest.mark.parametrize(
    ("metric", "unit", "value", "threshold_text"),
    [
        ("error_rate", "ratio", 0.14, "above the absolute threshold 5.0%"),
        ("latency_p95", "seconds", 9.5, "above the absolute threshold 2.00s"),
        ("db_pool_utilization", "ratio", 1.0, "above the absolute threshold 90.0%"),
        ("db_pool_pending", "count", 11.0, "above the absolute threshold 0"),
        ("cache_up", "bool", 0.0, "below the absolute threshold 0.5"),
    ],
)
def test_no_baseline_falls_back_to_absolute_thresholds(
    metric: str, unit: str, value: float, threshold_text: str
) -> None:
    """The live-demo bug: a fresh cluster (no baseline) with 14 % 5xx, a full DB pool and
    9.5 s p95 must NOT be reported as "no anomaly"."""
    stats = detect(metric, "payment-service", unit, series(FRESH + [value] * 31), WINDOW, DEFAULTS)
    a = stats.anomaly
    assert a is not None, stats.note
    assert a.baseline is None and stats.baseline is None  # never invented
    assert a.threshold == DEFAULTS[metric] and a.current == value
    assert a.start_time == WINDOW.start and a.ongoing
    assert f"no baseline; {threshold_text}" in a.describe()
    assert stats.as_dict()["basis"] == "absolute"
    assert stats.as_dict()["threshold"] == DEFAULTS[metric]
    assert "->" not in a.describe()  # no fake "baseline -> current"


def test_healthy_values_without_baseline_are_not_anomalies() -> None:
    """S0 on a fresh cluster stays silent: healthy absolute values are below every limit."""
    healthy = {
        "error_rate": ("ratio", 0.0),
        "latency_p95": ("seconds", 0.975),  # the sample services' healthy top bucket
        "latency_p99": ("seconds", 0.995),
        "db_pool_utilization": ("ratio", 0.35),
        "db_pool_pending": ("count", 0.0),
        "cache_up": ("bool", 1.0),
    }
    stats = []
    for metric, (unit, value) in healthy.items():
        s = detect(metric, "payment-service", unit, series(FRESH + [value] * 31), WINDOW, DEFAULTS)
        assert s.anomaly is None, (metric, s.note)
        assert s.note and "no baseline" in s.note
        stats.append(s)
    analysis = MetricsAnalysis("payment-service", [], WINDOW, WINDOW, stats=stats)
    assert analysis.signals == ["no_anomaly"]
    assert "within the absolute thresholds" in analysis.no_anomaly_sentence()


def test_absolute_threshold_needs_a_sustained_run() -> None:
    blip = FRESH + [0.0] * 10 + [0.5, 0.5] + [0.0] * 19  # two points (1 min) above 5 %
    stats = detect("error_rate", "payment-service", "ratio", series(blip), WINDOW, DEFAULTS)
    assert stats.anomaly is None
    assert stats.note and "not above the absolute threshold 5.0%" in stats.note


def test_a_real_baseline_wins_over_absolute_thresholds() -> None:
    """Already at 100 % pool use before the window: that is the baseline, not a change."""
    values = series([1.0] * 61)
    stats = detect("db_pool_utilization", "payment-service", "ratio", values, WINDOW, DEFAULTS)
    assert stats.anomaly is None and stats.baseline == 1.0


def test_absolute_threshold_settings() -> None:
    assert DEFAULTS == {
        "error_rate": 0.05,
        "latency_p95": 2.0,
        "db_pool_utilization": 0.9,
        "db_pool_pending": 0.0,
        "cache_up": 0.5,
    }
    custom = absolute_thresholds({"latency_p95": 0.5, "latency_p99": 1, "error_rate": None})
    assert custom["latency_p95"] == 0.5 and custom["latency_p99"] == 1.0
    assert "error_rate" not in custom  # null disables one
    with pytest.raises(ConfigError, match=r"unknown metric.*rps"):
        absolute_thresholds({"rps": 1})
    with pytest.raises(ConfigError, match="latency_p95 must be a non-negative number"):
        absolute_thresholds({"latency_p95": "2s"})
    with pytest.raises(ConfigError, match="must be a mapping"):
        absolute_thresholds([1])


def test_noise_and_short_blips_are_not_anomalies() -> None:
    noisy = [0.97 + (0.03 if i % 3 else -0.02) for i in range(61)]
    assert detect("latency_p95", "s", "seconds", series(noisy), WINDOW).anomaly is None
    blip = flat_then(0.0, 0.0)
    blip[45] = blip[46] = 0.5  # 1 minute only
    assert detect("error_rate", "s", "ratio", series(blip), WINDOW).anomaly is None
    # an anomaly entirely inside the baseline window is not reported
    early = flat_then(0.5, 0.0, change_at=20)
    early[:10] = [0.0] * 10
    assert detect("error_rate", "s", "ratio", series(early), WINDOW).anomaly is None


def test_latency_ratio_zscore_and_recovery() -> None:
    values = [0.95 + 0.01 * (i % 3) for i in range(61)]
    for i in range(40, 52):
        values[i] = 5.0
    a = detect("latency_p95", "s", "seconds", series(values), WINDOW).anomaly
    assert a is not None and not a.ongoing
    assert a.ratio == pytest.approx(5.0 / 0.96, rel=0.01)
    assert a.zscore is not None and a.zscore > 100


def test_traffic_drop_and_gaps() -> None:
    values: list[float | None] = flat_then(3.0, 0.5)
    values[50] = None  # scrape gap: skipped, not a break
    a = detect("rps", "s", "rps", series(values), WINDOW).anomaly
    assert a is not None and a.direction == "down"


def test_cache_down_and_pool_saturation() -> None:
    cache = detect("cache_up", "s", "bool", series(flat_then(1.0, 0.0)), WINDOW)
    assert cache.anomaly is not None and fmt(cache.anomaly.current, "bool") == "DOWN"
    pool = detect("db_pool_utilization", "s", "ratio", series(flat_then(0.1, 1.0)), WINDOW)
    assert pool.anomaly is not None


def test_restarts_and_oom() -> None:
    restarts = restarts_stats(
        "order-service", series(flat_then(0, 0, n=40) + [1] * 10 + [3] * 11), WINDOW
    )
    assert restarts.current == 3 and restarts.anomaly is not None
    assert restarts.note == "3 container restart(s) in the window"
    # an OOM kill already present in the baseline is not new
    old = oom_stats("order-service", series([1.0] * 61), WINDOW)
    assert old.anomaly is None
    new = oom_stats("order-service", series([None] * 45 + [1.0] * 16), WINDOW)
    assert new.anomaly is not None and new.note == "container OOMKilled in the window"


def test_signals() -> None:
    analysis = MetricsAnalysis(
        service="order-service",
        dependencies=["inventory-service", "payment-service"],
        window=WINDOW,
        baseline_window=TimeRange(start=T0, end=WINDOW.start),
    )
    assert analysis.signals == ["no_anomaly"]
    assert "No metric anomaly for order-service" in "\n".join(analysis.lines())
    analysis.stats = [
        detect("latency_p95", "inventory-service", "seconds", series(flat_then(0.03, 3.5)), WINDOW),
        detect("latency_p95", "order-service", "seconds", series(flat_then(0.97, 5.0, 42)), WINDOW),
        detect("error_rate", "order-service", "ratio", series(flat_then(0.0, 0.4, 43)), WINDOW),
        detect("cache_up", "payment-service", "bool", series(flat_then(1.0, 0.0)), WINDOW),
    ]
    assert analysis.signals == [
        "error_rate_up",
        "latency_up",
        "cache_down",
        "dependency_latency_up",
    ]
    first = analysis.first_anomaly
    assert first is not None and first.metric == "latency_p95"
    text = "\n".join(analysis.lines())
    assert "Dependency inventory-service (1):" in text
    analysis.missing = ["rps"]
    analysis.stats = []
    assert analysis.signals == []  # a failed query is not an all-clear


def test_downsample() -> None:
    points = downsample(series([float(i) for i in range(181)]))
    assert len(points) <= 42 and points[-1][1] == 180.0
