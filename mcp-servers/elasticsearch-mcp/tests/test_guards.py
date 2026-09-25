from __future__ import annotations

import pytest

from elasticsearch_mcp.guards import (
    GuardError,
    clamp_size,
    validate_esql,
    validate_index,
    validate_time_range,
)

ALLOWED = ("payment-prod-*", "order-prod-*")


@pytest.mark.parametrize(
    "index", ["payment-prod-*", "payment-prod-2026.09.25", "payment-prod-*,order-prod-*"]
)
def test_allowed_indices(index: str) -> None:
    assert validate_index(index, ALLOWED)


@pytest.mark.parametrize(
    "index",
    [
        "*",
        "_all",
        ".security",
        "-payment-prod-*",
        "user-prod-*",
        "payment-*",
        "payment-prod-*,secrets",
        "",
    ],
)
def test_rejected_indices(index: str) -> None:
    with pytest.raises(GuardError):
        validate_index(index, ALLOWED)


def test_time_range() -> None:
    window = validate_time_range("2026-09-25T10:00:00Z", "2026-09-25T10:30:00Z", 48)
    assert (window.end - window.start).total_seconds() == 1800
    assert window.as_filter("@timestamp")["range"]["@timestamp"]["gte"].startswith(
        "2026-09-25T10:00"
    )


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2026-09-25T10:30:00Z", "2026-09-25T10:00:00Z", "after start"),
        ("2026-09-20T00:00:00Z", "2026-09-25T00:00:00Z", "maximum"),
        ("yesterday", "2026-09-25T00:00:00Z", "ISO-8601"),
    ],
)
def test_bad_time_ranges(start: str, end: str, message: str) -> None:
    with pytest.raises(GuardError, match=message):
        validate_time_range(start, end, 48)


def test_naive_times_are_utc() -> None:
    window = validate_time_range("2026-09-25T10:00:00", "2026-09-25T11:00:00", 48)
    assert window.start.tzinfo is not None


def test_esql() -> None:
    assert validate_esql(
        'FROM payment-prod-* | WHERE level == "ERROR" | STATS COUNT(*)', ALLOWED
    ) == ["payment-prod-*"]
    for bad, msg in [
        ("ROW a = 1", "must start with"),
        ("FROM .security | LIMIT 1", "not allowed"),
        ("FROM user-prod-* | LIMIT 1", "outside"),
        ("FROM payment-prod-* | ENRICH policy", "ENRICH"),
        ("FROM payment-prod-* | LOOKUP JOIN users ON id", "LOOKUP"),
    ]:
        with pytest.raises(GuardError, match=msg):
            validate_esql(bad, ALLOWED)


def test_clamp_size() -> None:
    assert clamp_size(5000, 1000) == 1000
    with pytest.raises(GuardError):
        clamp_size(0, 1000)
