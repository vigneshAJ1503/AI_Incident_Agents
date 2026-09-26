"""Requires Alertmanager + alertmanager-mcp (`make alertmanager-up`, then
`docker compose ... -f deploy/compose/docker-compose.mcp.yml up -d --build alertmanager-mcp`).
Run with: make test-integration"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiops.core.config import load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.mcp.registry import MCPRegistry
from aiops.seed.alertmanager import AlertmanagerSeeder
from aiops.seed.alerts import scenario_alerts
from aiops.seed.logs import SeedWindow

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "config"
AM_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")


def seed(scenario: str, now: datetime) -> None:
    seeder = AlertmanagerSeeder(AM_URL)
    try:
        seeder.clear()
        seeder.post(scenario_alerts(scenario, SeedWindow.build(now, hours=1)))
    finally:
        seeder.close()


async def test_alerts_capability_end_to_end() -> None:
    now = datetime.now(UTC)
    seed("S1", now)
    settings = load_settings("local", CONFIG)
    audit = MemoryAuditSink()
    try:
        async with MCPRegistry(settings, audit=audit).toolset("alerts", agent="it") as tools:
            names = {s.name for s in await tools.specs()}
            assert names == {
                "list_alerts",
                "get_alert",
                "list_silences",
                "get_alert_groups",
                "alert_history",
            }
            listed = await tools.call(
                "list_alerts", {"labels": {"service": "payment-service", "namespace": "prod"}}
            )
            assert listed.ok, listed.content
            alerts = listed.data["alerts"]
            assert [a["alertname"] for a in alerts] == [
                "DatabaseConnectionPoolExhausted",
                "HighErrorRate",
            ]
            one = await tools.call("get_alert", {"fingerprint": alerts[1]["fingerprint"]})
            assert one.ok and one.data["alert"]["severity"] == "critical"
            groups = await tools.call("get_alert_groups", {"service": "payment-service"})
            assert groups.ok and groups.data["group_count"] == 2
            silences = await tools.call("list_silences", {"service": "payment-service"})
            assert silences.ok
            history = await tools.call(
                "alert_history",
                {
                    "start": (now - timedelta(hours=1)).isoformat(),
                    "end": now.isoformat(),
                    "service": "payment-service",
                },
            )
            assert history.ok, history.content
            # complete (resolved alerts included) only when the server reads Prometheus ALERTS
            assert history.data["complete"] is ("prometheus" in history.data["sources"])
            # seeded alerts bypass Prometheus: they still show up, from Alertmanager
            assert {"DatabaseConnectionPoolExhausted", "HighErrorRate"} <= {
                a["alertname"] for a in history.data["alerts"]
            }
            guarded = await tools.call("list_alerts", {"labels": {"service=~": ".+"}})
            assert not guarded.ok and "invalid label name" in guarded.content
    finally:
        seed("S0", now)
    assert [r.tool_call.status for r in audit.records][-1] == "error"
