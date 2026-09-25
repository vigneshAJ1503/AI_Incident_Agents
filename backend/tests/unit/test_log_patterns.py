from __future__ import annotations

import pytest

from aiops.agents.log_agent.patterns import cluster, like_prefix, template


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (
            "Database connection timeout: could not acquire a connection from the pool within 5000ms "
            "(pool size=2, active=2, waiting=17)",
            "Database connection timeout: could not acquire a connection from the pool within <NUM>ms "
            "(pool size=<NUM>, active=<NUM>, waiting=<NUM>)",
        ),
        (
            "Timeout calling inventory-service GET /api/v1/stock/4411 after 3000ms",
            "Timeout calling inventory-service GET /api/v1/stock/<NUM> after <NUM>ms",
        ),
        (
            "Redis connection refused: redis:6379 (ECONNREFUSED)",
            "Redis connection refused: redis:<NUM> (ECONNREFUSED)",
        ),
        (
            "user 3f2a1c9e-8b7d-4e6f-9a0b-1c2d3e4f5a6b from 10.0.0.12:443 failed",
            "user <UUID> from <IP> failed",
        ),
        ("trace 96a931cfdc9709fe at 2026-09-25T10:10:07.571Z", "trace <HEX> at <TS>"),
        (
            "HikariPool-1 - Connection is not available",
            "HikariPool-<NUM> - Connection is not available",
        ),
        ("payment-service v1.8.2 started", "payment-service v1.8.2 started"),  # versions kept
    ],
)
def test_template(message: str, expected: str) -> None:
    assert template(message) == expected


def test_cluster_merges_variants() -> None:
    rows = [
        (6, "2026-09-25T10:13:50Z", "2026-09-25T10:27:51Z", "ERROR", "pool waiting=10"),
        (5, "2026-09-25T10:10:07Z", "2026-09-25T10:29:00Z", "ERROR", "pool waiting=11"),
        (2, "2026-09-25T10:11:00Z", "2026-09-25T10:12:00Z", "WARN", "pool waiting=12"),
        (1, "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z", "ERROR", "other"),
    ]
    patterns = cluster(rows)
    top = patterns[0]
    assert (top.template, top.level, top.count) == ("pool waiting=<NUM>", "ERROR", 11)
    assert top.first_seen == "2026-09-25T10:10:07Z" and top.last_seen == "2026-09-25T10:29:00Z"
    assert top.examples == ["pool waiting=10", "pool waiting=11"]
    assert [(p.level, p.count) for p in patterns[1:]] == [("WARN", 2), ("ERROR", 1)]


def test_like_prefix() -> None:
    assert (
        like_prefix("Database connection timeout: within <NUM>ms")
        == "Database connection timeout: within"
    )
    assert like_prefix("<NUM> errors") is None
    assert like_prefix("Short <NUM>") is None
