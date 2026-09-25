from __future__ import annotations

import pytest

from alertmanager_mcp.guards import (
    GuardError,
    build_matchers,
    clamp_limit,
    merge_filters,
    state_flags,
    validate_fingerprint,
    validate_time_range,
)
from alertmanager_mcp.server import silence_could_apply


def test_build_matchers_sorted_and_escaped() -> None:
    assert build_matchers({"service": "a", "alertname": 'x"y\\z'}, 5) == [
        'alertname="x\\"y\\\\z"',
        'service="a"',
    ]
    assert build_matchers({}, 5) == []


@pytest.mark.parametrize("name", ["1abc", "a-b", "", "a b", "x" * 200, "service=~"])
def test_invalid_label_names(name: str) -> None:
    with pytest.raises(GuardError, match="invalid label name"):
        build_matchers({name: "v"}, 5)


def test_value_limits() -> None:
    with pytest.raises(GuardError, match="longer than"):
        build_matchers({"service": "x" * 300}, 5)


def test_merge_filters() -> None:
    assert merge_filters({"namespace": "prod"}, service="s", severity=None) == {
        "namespace": "prod",
        "service": "s",
    }
    assert merge_filters({"service": "s"}, service="s") == {"service": "s"}
    with pytest.raises(GuardError, match="conflicting"):
        merge_filters({"severity": "warning"}, severity="critical")


def test_states_limits_fingerprints_and_ranges() -> None:
    assert state_flags("active").silenced is False
    assert state_flags("all").inhibited is True
    with pytest.raises(GuardError, match="state must be one of"):
        state_flags("firing")
    assert clamp_limit(500, 200) == 200
    assert validate_fingerprint(" 45410293E243BA8C ") == "45410293e243ba8c"
    start, end = validate_time_range("2026-09-25T10:00:00", "2026-09-25T10:30:00Z", 1)
    assert start.tzinfo is not None and (end - start).total_seconds() == 1800
    with pytest.raises(GuardError, match="after start"):
        validate_time_range("2026-09-25T10:30:00Z", "2026-09-25T10:00:00Z", 1)


def test_silence_applicability() -> None:
    silence = {
        "matchers": [
            {"name": "service", "value": "order-.*", "isEqual": True, "isRegex": True},
            {"name": "namespace", "value": "prod", "isEqual": True, "isRegex": False},
        ]
    }
    assert silence_could_apply(silence, {"service": "order-service"})
    assert not silence_could_apply(silence, {"service": "payment-service"})
    assert silence_could_apply(silence, {})  # unknown labels don't exclude
    broken = {"matchers": [{"name": "service", "value": "(", "isRegex": True}]}
    assert silence_could_apply(broken, {"service": "x"})
