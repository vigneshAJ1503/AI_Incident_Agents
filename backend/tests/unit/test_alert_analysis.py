from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aiops.agents.alert_agent.agent import matcher_filter
from aiops.agents.alert_agent.analysis import alert_views, analyze, minutes, parse_ts
from aiops.core.models import TimeRange

WINDOW = TimeRange(
    start=datetime(2026, 9, 25, 10, 0, tzinfo=UTC), end=datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
)


def alert(name: str, service: str, severity: str, starts: str, state: str = "active") -> dict:
    return {
        "alertname": name,
        "service": service,
        "severity": severity,
        "state": state,
        "starts_at": starts,
        "summary": f"{service}: {name}",
        "runbook_url": None,
        "fingerprint": "f",
        "labels": {},
    }


def run(
    own: list[dict[str, Any]],
    deps: dict[str, list[dict[str, Any]]] | None = None,
    silences: list[dict[str, Any]] | None = None,
    failed: list[str] | None = None,
    start: datetime | None = None,
):  # type: ignore[no-untyped-def]
    return analyze(
        {"alerts": own},
        {name: {"alerts": a} for name, a in (deps or {}).items()},
        {"silences": silences or []},
        service="payment-service",
        window=WINDOW,
        incident_start=start or WINDOW.start,
        incident_start_source="window start",
        critical_severities=["critical", "P1"],
        failed_scopes=failed,
    )


def test_healthy() -> None:
    analysis = run([], {"redis": []})
    assert analysis.signals == ["no_active_alerts"]
    assert analysis.lines()[1] == (
        "No active alerts for payment-service or its dependencies (redis): "
        "no alert threshold was breached."
    )


def test_suppressed_and_later_alerts_are_not_firing() -> None:
    analysis = run(
        [
            alert(
                "HighErrorRate", "payment-service", "critical", "2026-09-25T10:12:00Z", "suppressed"
            ),
            alert("HighLatencyP95", "payment-service", "warning", "2026-09-25T11:00:00Z"),
        ],
        silences=[{"id": "s1", "matchers": ['service="payment-service"'], "ends_at": "x"}],
    )
    assert analysis.firing == []
    assert analysis.signals == ["no_active_alerts"]
    text = "\n".join(analysis.lines())
    assert "Suppressed (silenced or inhibited) alerts (1):" in text
    assert "Alerts that started after the window" in text and "HighLatencyP95" in text
    assert "Active silences that could apply (1):" in text


def test_dependency_critical_and_precedence() -> None:
    analysis = run(
        [alert("HighLatencyP95", "payment-service", "warning", "2026-09-25T10:16:00Z")],
        {"redis": [alert("RedisDown", "redis", "P1", "2026-09-25T09:55:00Z")]},
    )
    assert analysis.signals == [
        "alerts_firing",
        "critical_alert_firing",
        "dependency_alert_firing",
        "alert_precedes_incident",
    ]
    text = "\n".join(analysis.lines())
    assert "(-5m vs incident start, before the window)" in text
    assert "First alert to fire: RedisDown on redis at 2026-09-25T09:55:00Z." in text


def test_only_dependency_firing() -> None:
    analysis = run([], {"redis": [alert("RedisDown", "redis", "critical", "2026-09-25T10:11:00Z")]})
    assert analysis.signals == ["critical_alert_firing", "dependency_alert_firing"]
    assert "No firing alerts on payment-service itself." in analysis.lines()


def test_failed_scope_never_claims_all_clear() -> None:
    analysis = run([], {}, failed=["redis"])
    assert analysis.signals == []
    assert "Could not query alerts for: redis." in analysis.lines()


def test_robust_parsing() -> None:
    assert alert_views(None, "x", is_dependency=False) == []
    assert alert_views({"alerts": ["junk", {}]}, "x", is_dependency=True)[0].alertname == "unknown"
    assert parse_ts("not a time") is None and parse_ts(None) is None
    assert parse_ts("2026-09-25T10:00:00") == WINDOW.start
    assert minutes(-240) == "-4m" and minutes(90) == "+2m"
    assert matcher_filter({"service": "a", "namespace": "prod"}) == (
        '{namespace="prod",service="a"}'
    )
