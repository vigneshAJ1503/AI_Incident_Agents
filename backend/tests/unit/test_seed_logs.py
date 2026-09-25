from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest

from aiops.seed.logs import SCENARIOS, LogGenerator, SeedWindow, index_name, index_pattern

NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
WINDOW = SeedWindow.build(NOW, hours=26)


def docs(scenario: str, seed: int = 42) -> list[dict]:
    return list(LogGenerator(scenario, WINDOW, seed=seed).generate())


def incident(ds: list[dict]) -> list[dict]:
    start = WINDOW.incident_start.isoformat().replace("+00:00", "Z")
    return [d for d in ds if d["@timestamp"] >= start]


def test_deterministic() -> None:
    assert docs("S1") == docs("S1")
    assert docs("S1") != docs("S1", seed=7)


def test_volume_and_time_bounds() -> None:
    ds = docs("S0")
    assert 40_000 < len(ds) < 80_000
    first = min(d["@timestamp"] for d in ds)
    last = max(d["@timestamp"] for d in ds)
    assert first >= WINDOW.start.isoformat().replace("+00:00", "Z")
    assert last < NOW.isoformat().replace("+00:00", "Z")
    assert {d["service"] for d in ds} == {
        "payment-service",
        "order-service",
        "user-service",
        "inventory-service",
    }


def test_s0_has_only_background_noise() -> None:
    recent = incident(docs("S0"))
    errors = [d for d in recent if d["level"] == "ERROR"]
    assert all("declined" in d["message"] for d in errors)
    assert len(errors) < 10


def test_s1_db_pool_signature() -> None:
    ds = docs("S1")
    recent = incident(ds)
    timeouts = [d for d in recent if "Database connection timeout" in d["message"]]
    assert len(timeouts) > 50
    assert all(d["service"] == "payment-service" and d["status_code"] == 500 for d in timeouts)
    assert all(d["version"] == "v1.8.2" for d in timeouts)
    # pattern is new: none before the incident
    assert not [d for d in ds if "Database connection timeout" in d["message"] and d not in recent]
    starts = [
        d["message"] for d in ds if d["message"].startswith("Starting payment-service v1.8.2")
    ]
    assert starts and "DB_POOL_SIZE=2" in starts[0]
    assert any(
        "Payment call failed" in d["message"] for d in recent if d["service"] == "order-service"
    )


@pytest.mark.parametrize(
    ("scenario", "service", "needle"),
    [
        ("S2", "order-service", "OutOfMemoryError"),
        ("S3", "order-service", "Timeout calling inventory-service"),
        ("S3", "inventory-service", "Slow query"),
        ("S4", "user-service", "ready replicas 1/3"),
        ("S5", "payment-service", "Redis connection refused"),
    ],
)
def test_scenario_signatures(scenario: str, service: str, needle: str) -> None:
    recent = incident(docs(scenario))
    assert sum(1 for d in recent if d["service"] == service and needle in d["message"]) > 5


def test_every_scenario_generates() -> None:
    for scenario in SCENARIOS:
        assert docs(scenario)


def test_index_naming() -> None:
    assert index_name("payment-service", "production", NOW) == "payment-prod-2026.09.25"
    assert index_pattern("payment-service", "production") == "payment-prod-*"


def test_baseline_window_exists() -> None:
    ds = docs("S1")
    baseline_end = (NOW - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    baseline_start = (NOW - timedelta(hours=24, minutes=30)).isoformat().replace("+00:00", "Z")
    baseline = [d for d in ds if baseline_start <= d["@timestamp"] < baseline_end]
    assert Counter(d["service"] for d in baseline)["payment-service"] > 200


def test_unknown_scenario() -> None:
    with pytest.raises(ValueError, match="Unknown scenario"):
        LogGenerator("S9", WINDOW)
