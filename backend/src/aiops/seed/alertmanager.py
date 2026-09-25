"""Post seeded alerts to a local Alertmanager (API v2, plain REST)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx2

from aiops.seed.alerts import DEFAULT_TTL, GENERATOR_PREFIX, resolved, scenario_alerts
from aiops.seed.elasticsearch import SeedError
from aiops.seed.logs import SeedWindow


class AlertmanagerSeeder:
    def __init__(
        self, url: str, *, timeout_s: float = 30.0, transport: httpx2.BaseTransport | None = None
    ) -> None:
        self._client = httpx2.Client(
            base_url=url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def _check(self, response: httpx2.Response, action: str) -> Any:
        if response.status_code >= 400:
            raise SeedError(f"{action} failed: HTTP {response.status_code} {response.text[:300]}")
        return response.json() if response.content else {}

    def ping(self) -> str:
        try:
            status = self._check(self._client.get("/api/v2/status"), "status")
        except httpx2.HTTPError as exc:
            raise SeedError(
                f"Alertmanager not reachable: {exc}. Run `make alertmanager-up`."
            ) from exc
        return str(status.get("versionInfo", {}).get("version", "?"))

    def seeded_alerts(self) -> list[dict[str, Any]]:
        """Active alerts created by an earlier seed (marked by their generatorURL)."""
        alerts = self._check(self._client.get("/api/v2/alerts"), "list alerts")
        return [a for a in alerts if str(a.get("generatorURL", "")).startswith(GENERATOR_PREFIX)]

    def post(self, alerts: list[dict[str, Any]]) -> None:
        if alerts:
            self._check(self._client.post("/api/v2/alerts", json=alerts), "post alerts")

    def clear(self) -> int:
        """Resolve every previously seeded alert; returns how many were resolved."""
        previous = self.seeded_alerts()
        self.post(resolved(previous))
        return len(previous)


def seed_scenario_alerts(
    url: str,
    scenario: str,
    now: datetime,
    *,
    ttl: timedelta = DEFAULT_TTL,
    environment: str = "production",
) -> tuple[SeedWindow, list[dict[str, Any]]]:
    """Replace the seeded alerts with ``scenario``'s, anchored at ``now`` (S0 = none)."""
    window = SeedWindow.build(now, hours=1)
    alerts = scenario_alerts(scenario, window, ttl=ttl, environment=environment)
    seeder = AlertmanagerSeeder(url)
    try:
        seeder.ping()
        seeder.clear()
        seeder.post(alerts)
    finally:
        seeder.close()
    return window, alerts
