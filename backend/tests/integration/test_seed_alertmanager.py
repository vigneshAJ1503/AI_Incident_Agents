"""Requires Alertmanager (`make alertmanager-up`). Run with: make test-integration"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import httpx2
import pytest

from aiops.seed.alertmanager import AlertmanagerSeeder
from aiops.seed.alerts import scenario_alerts
from aiops.seed.logs import SeedWindow

pytestmark = pytest.mark.integration
AM_URL = os.environ.get("ALERTMANAGER_URL", "http://localhost:9093")


def active(scenario: str) -> set[tuple[str, str]]:
    alerts = httpx2.get(f"{AM_URL}/api/v2/alerts", params={"active": "true"}).json()
    return {
        (a["labels"]["alertname"], a["labels"]["service"])
        for a in alerts
        if a.get("generatorURL", "").startswith(f"aiops-seed://{scenario}/")
    }


def seed(scenario: str, now: datetime) -> None:
    seeder = AlertmanagerSeeder(AM_URL)
    try:
        seeder.ping()
        seeder.clear()
        seeder.post(scenario_alerts(scenario, SeedWindow.build(now, hours=1)))
    finally:
        seeder.close()


def test_seed_replace_and_clear() -> None:
    now = datetime.now(UTC)
    seed("S2", now)
    assert ("PodOOMKilled", "order-service") in active("S2")

    # Re-seeding the same alerts with another anchor must not leave them resolved.
    seed("S1", datetime(2026, 9, 25, 10, 30, tzinfo=UTC))
    seed("S1", now)
    assert active("S2") == set()
    assert active("S1") == {
        ("HighErrorRate", "payment-service"),
        ("DatabaseConnectionPoolExhausted", "payment-service"),
    }

    seed("S0", now)
    assert active("S1") == set()
