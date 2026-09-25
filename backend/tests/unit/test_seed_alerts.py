from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from typer.testing import CliRunner

from aiops.cli import seed_cmd
from aiops.cli.main import app
from aiops.seed.alertmanager import AlertmanagerSeeder
from aiops.seed.alerts import (
    GENERATOR_PREFIX,
    RULES,
    SCENARIO_ALERTS,
    resolved,
    scenario_alerts,
)
from aiops.seed.elasticsearch import SeedError
from aiops.seed.logs import SCENARIOS, SeedWindow

NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
WALL = datetime(2026, 9, 26, 8, 0, tzinfo=UTC)
WINDOW = SeedWindow.build(NOW, hours=1)
RULES_FILE = (
    Path(__file__).resolve().parents[3] / "deploy/compose/config/prometheus/alert-rules.yml"
)


def names(scenario: str) -> set[tuple[str, str]]:
    return {(a["labels"]["alertname"], a["labels"]["service"]) for a in alerts(scenario)}


def alerts(scenario: str) -> list[dict[str, Any]]:
    return scenario_alerts(scenario, WINDOW, wall_now=WALL)


def test_scenario_mapping() -> None:
    assert alerts("S0") == []
    assert names("S1") == {
        ("HighErrorRate", "payment-service"),
        ("DatabaseConnectionPoolExhausted", "payment-service"),
    }
    assert {"PodOOMKilled", "PodCrashLooping"} <= {n for n, _ in names("S2")}
    assert ("HighLatencyP95", "inventory-service") in names("S3")
    assert ("HighLatencyP95", "order-service") in names("S3")
    assert names("S4") == {("DeploymentReplicasMismatch", "user-service")}
    assert ("RedisDown", "redis") in names("S5")
    assert ("HighLatencyP95", "payment-service") in names("S5")
    with pytest.raises(ValueError, match="Unknown scenario"):
        scenario_alerts("S9", WINDOW)
    with pytest.raises(ValueError, match="environment"):
        scenario_alerts("S1", WINDOW, environment="qa")


def test_timeline_labels_and_expiry() -> None:
    by_name = {a["labels"]["alertname"]: a for a in alerts("S1")}
    pool, errors = by_name["DatabaseConnectionPoolExhausted"], by_name["HighErrorRate"]
    # incident at now-20m (10:10); the pool alert fires first, both after the 10:08 rollout
    assert pool["startsAt"] == "2026-09-25T10:11:00.000Z"
    assert errors["startsAt"] == "2026-09-25T10:12:00.000Z"
    # alive for 24h from the WALL clock, independent of the anchor
    assert errors["endsAt"] == "2026-09-27T08:00:00.000Z"
    assert errors["labels"] == {
        "alertname": "HighErrorRate",
        "service": "payment-service",
        "severity": "critical",
        "team": "payments",
        "namespace": "prod",
    }
    assert errors["annotations"]["summary"] == "payment-service: 24.6% of requests fail with 5xx"
    assert errors["annotations"]["runbook_url"] == "knowledge-base/runbooks/high-error-rate.md"
    assert errors["generatorURL"] == f"{GENERATOR_PREFIX}S1/HighErrorRate"
    short = scenario_alerts("S1", WINDOW, wall_now=WALL, ttl=timedelta(hours=1))
    assert short[0]["endsAt"] == "2026-09-26T09:00:00.000Z"
    staging = scenario_alerts("S2", WINDOW, environment="staging")
    assert {a["labels"]["namespace"] for a in staging} == {"staging"}


def test_all_incident_alerts_start_inside_the_incident() -> None:
    for scenario in SCENARIOS:
        for alert in alerts(scenario):
            starts = datetime.fromisoformat(alert["startsAt"].replace("Z", "+00:00"))
            assert WINDOW.incident_start <= starts < WINDOW.now, (scenario, alert["labels"])


def _go_to_python(template: str) -> str:
    """Translate the rule templates' Go syntax into our str.format placeholders."""
    template = re.sub(r"\{\{\s*\$value[^}]*\}\}", "{value}", template)
    template = template.replace("{{ $labels.label_app }}", "{service}")
    template = re.sub(r"\{\{\s*\$labels\.(\w+)\s*\}\}", r"{\1}", template)
    return " ".join(template.split())


def test_seeded_alerts_match_the_prometheus_rules() -> None:
    rules = {
        r["alert"]: r
        for group in yaml.safe_load(RULES_FILE.read_text())["groups"]
        for r in group["rules"]
    }
    assert set(rules) == set(RULES)
    used = {spec.alertname for specs in SCENARIO_ALERTS.values() for spec in specs}
    assert used <= set(rules)
    for name, template in RULES.items():
        rule = rules[name]
        assert rule["labels"]["severity"] == template.severity, name
        annotations = rule["annotations"]
        assert annotations["runbook_url"] == template.runbook_url, name
        assert _go_to_python(annotations["summary"]) == template.summary, name
        assert _go_to_python(annotations["description"]) == template.description, name


def test_resolved_payload_uses_zero_length_range() -> None:
    active = [{**alerts("S1")[0], "fingerprint": "abc", "status": {"state": "active"}}]
    [payload] = resolved(active)
    assert payload["endsAt"] == payload["startsAt"] == active[0]["startsAt"]
    assert payload["labels"] == active[0]["labels"]
    assert set(payload) == {"labels", "annotations", "startsAt", "endsAt", "generatorURL"}


class FakeAlertmanager:
    def __init__(self, stored: list[dict[str, Any]]) -> None:
        self.stored = stored
        self.posts: list[list[dict[str, Any]]] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/v2/status":
            return httpx2.Response(200, json={"versionInfo": {"version": "0.34.1"}})
        if request.url.path == "/api/v2/alerts" and request.method == "GET":
            return httpx2.Response(200, json=self.stored)
        if request.url.path == "/api/v2/alerts" and request.method == "POST":
            self.posts.append(json.loads(request.content))
            return httpx2.Response(200)
        return httpx2.Response(404, text="not found")


def test_seeder_clears_only_seeded_alerts() -> None:
    real = {**alerts("S1")[0], "generatorURL": "http://prometheus:9090/graph?g0.expr=up"}
    fake = FakeAlertmanager([*alerts("S3"), real])
    seeder = AlertmanagerSeeder("http://am", transport=httpx2.MockTransport(fake))
    try:
        assert seeder.ping() == "0.34.1"
        assert seeder.clear() == 3
        seeder.post([])  # nothing to post -> no request
    finally:
        seeder.close()
    [posted] = fake.posts
    assert {a["labels"]["alertname"] for a in posted} == {"HighLatencyP95", "HighErrorRate"}
    assert all(a["generatorURL"].startswith(GENERATOR_PREFIX) for a in posted)
    assert all(a["endsAt"] == a["startsAt"] for a in posted)


def test_seeder_errors() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    seeder = AlertmanagerSeeder("http://am", transport=httpx2.MockTransport(refuse))
    with pytest.raises(SeedError, match="not reachable"):
        seeder.ping()
    bad = AlertmanagerSeeder(
        "http://am",
        transport=httpx2.MockTransport(lambda r: httpx2.Response(400, text="bad alert")),
    )
    with pytest.raises(SeedError, match="HTTP 400 bad alert"):
        bad.post(alerts("S1"))


def test_cli_seed_alerts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAlertmanager(alerts("S2"))
    monkeypatch.setattr(
        seed_cmd,
        "AlertmanagerSeeder",
        lambda url: AlertmanagerSeeder(url, transport=httpx2.MockTransport(fake)),
    )
    result = CliRunner().invoke(
        app, ["seed", "alerts", "--scenario", "s1", "--now", "2026-09-25T10:30:00"]
    )
    assert result.exit_code == 0, result.output
    assert "2 firing" in result.output and "3 previously seeded" in result.output
    cleared, posted = fake.posts
    assert len(cleared) == 3
    assert {a["startsAt"] for a in posted} == {
        "2026-09-25T10:11:00.000Z",
        "2026-09-25T10:12:00.000Z",
    }
    bad = CliRunner().invoke(app, ["seed", "alerts", "--scenario", "S1", "--ttl-hours", "0"])
    assert bad.exit_code != 0
