from __future__ import annotations

from datetime import timedelta
from typing import Any

from aiops.agents.log_agent.analysis import analyze

LEVELS = ["ERROR", "FATAL", "CRITICAL"]
CUR, BASE = timedelta(minutes=30), timedelta(hours=24)


def volume(cur_err: int, cur_info: int, base_err: int, base_info: int) -> dict[str, Any]:
    return {
        "columns": ["count", "window", "level"],
        "rows": [
            [cur_err, "current", "ERROR"],
            [cur_info, "current", "INFO"],
            [base_err, "baseline", "ERROR"],
            [base_info, "baseline", "INFO"],
        ],
    }


def patterns(*rows: tuple[str, str, int, str]) -> dict[str, Any]:
    return {
        "columns": ["count", "first_seen", "last_seen", "window", "level", "msg"],
        "rows": [
            [c, "2026-09-25T10:10:00Z", "2026-09-25T10:29:00Z", w, lvl, m] for w, lvl, c, m in rows
        ],
    }


def lifecycle(*rows: tuple[str, str, int]) -> dict[str, Any]:
    return {
        "columns": ["count", "starts", "first_seen", "window", "version"],
        "rows": [[100, s, "2026-09-25T10:08:00Z", w, v] for w, v, s in rows],
    }


def test_new_db_pattern_with_deployment() -> None:
    analysis = analyze(
        volume(62, 300, 80, 17000),
        patterns(
            (
                "current",
                "ERROR",
                62,
                "Database connection timeout: could not acquire a connection (waiting=3)",
            ),
            ("baseline", "ERROR", 80, "Payment declined (order 1)"),
        ),
        lifecycle(("baseline", "v1.8.1", 3), ("current", "v1.8.1", 0), ("current", "v1.8.2", 3)),
        error_levels=LEVELS,
        current=CUR,
        baseline=BASE,
    )
    assert analysis.signals == [
        "db_timeout_errors_up",
        "error_rate_up",
        "new_error_pattern",
        "deployment_detected",
    ]
    assert analysis.deployments == [{"version": "v1.8.2", "first_seen": "2026-09-25T10:08:00Z"}]
    assert analysis.restarts == 0  # startups of a NEW version are a deployment, not restarts
    assert analysis.anomalous[0].is_new
    assert any("NEW" in line for line in analysis.lines())


def test_recurring_noise_is_not_anomalous() -> None:
    analysis = analyze(
        volume(1, 369, 65, 17000),
        patterns(
            ("current", "ERROR", 1, "Payment declined (order 7)"),
            ("baseline", "ERROR", 65, "Payment declined (order 3)"),
        ),
        lifecycle(("baseline", "v1.8.1", 3), ("current", "v1.8.1", 0)),
        error_levels=LEVELS,
        current=CUR,
        baseline=BASE,
    )
    assert analysis.signals == []
    assert analysis.anomalous == []


def test_elevated_recurring_pattern_is_anomalous() -> None:
    # 40 in 30m vs 48/24h (=1 per 30m expected) -> 40x normal
    analysis = analyze(
        volume(40, 300, 48, 17000),
        patterns(
            ("current", "ERROR", 40, "Slow query took 3000ms"),
            ("baseline", "ERROR", 48, "Slow query took 10ms"),
        ),
        None,
        error_levels=LEVELS,
        current=CUR,
        baseline=BASE,
    )
    assert analysis.anomalous and not analysis.anomalous[0].is_new
    assert analysis.anomalous[0].ratio >= 5
    assert "slow_queries" in analysis.signals and "new_error_pattern" not in analysis.signals


def test_restarts_on_existing_version() -> None:
    analysis = analyze(
        volume(0, 300, 0, 17000),
        patterns(),
        lifecycle(("baseline", "v2.3.0", 0), ("current", "v2.3.0", 5)),
        error_levels=LEVELS,
        current=CUR,
        baseline=BASE,
    )
    assert analysis.restarts == 5
    assert analysis.signals == ["service_restarts"]


def test_database_fallback_is_not_a_db_timeout() -> None:
    analysis = analyze(
        volume(20, 300, 0, 17000),
        patterns(
            (
                "current",
                "ERROR",
                20,
                "Cache unavailable, fell back to database (GET /x 200 in 90ms)",
            )
        ),
        None,
        error_levels=LEVELS,
        current=CUR,
        baseline=BASE,
    )
    assert "db_timeout_errors_up" not in analysis.signals
    assert "cache_connection_errors" in analysis.signals


def test_quiet_window_reports_no_errors() -> None:
    analysis = analyze(
        volume(0, 300, 0, 17000), patterns(), None, error_levels=LEVELS, current=CUR, baseline=BASE
    )
    assert analysis.signals == ["no_errors"]
