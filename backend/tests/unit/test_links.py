from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from aiops.core.config import load_settings
from aiops.core.links import duration, format_link, query_link, window_values

START = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
END = datetime(2026, 9, 25, 11, 30, tzinfo=UTC)
CONFIG = Path(__file__).resolve().parents[3] / "config"


def test_window_values() -> None:
    values = window_values(START, END)
    assert values == {
        "start": "2026-09-25T10:00:00Z",
        "end": "2026-09-25T11:30:00Z",
        "end_utc": "2026-09-25%2011:30:00",
        "from_ms": "1790330400000",
        "to_ms": "1790335800000",
        "range": "1h30m",
    }
    assert duration(45) == "45s" and duration(86400 + 61) == "1d1m1s" and duration(0) == "1s"


def test_format_link_never_raises() -> None:
    assert format_link(None, service="x") is None
    assert format_link("", service="x") is None
    assert format_link("https://g/{service}", service="x") == "https://g/x"
    assert format_link("https://g/{unknown}", service="x") is None
    assert format_link("https://g/{0}", service="x") is None


def test_metrics_capability_links() -> None:
    """The local.yaml templates: a Grafana panel link and a Prometheus graph link."""
    settings = load_settings("local", CONFIG).capability("metrics").settings
    panel = format_link(
        settings["ui_link_template"],
        service="payment-service",
        namespace="prod",
        panel=settings["panels"]["error_rate"],
        **window_values(START, END),
    )
    assert panel is not None
    assert "/d/aiops-service-overview/" in panel
    assert "var-service=payment-service&from=1790330400000&to=1790335800000&viewPanel=6" in panel
    explore = query_link(
        settings["explore_link_template"],
        'sum(rate(http_requests_total{service="payment-service"}[2m]))',
        START,
        END,
    )
    assert explore is not None
    assert "g0.expr=sum%28rate%28http_requests_total%7Bservice%3D%22payment-service" in explore
    assert "g0.range_input=1h30m&g0.end_input=2026-09-25%2011:30:00" in explore
