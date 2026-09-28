"""Severity rules in ONE place (orchestrator.severity) and replays on today's clock.

* The response builder rates severity from config: a catalog ``tier`` in
  ``critical_tiers`` + a measured error ratio >= ``critical_error_rate`` + a confident
  root cause -> ``critical``; user-facing impact elsewhere -> ``high``.
* A replay reported on another clock (``display_end``, e.g. now) moves every timestamp of
  the recorded data, so no recording date leaks into evidence, timeline or report.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiops.core.config import Settings, SeverityRules, load_settings
from aiops.core.models import Investigation
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import load_replay, replay_clock, shift_text

CONFIG = Path(__file__).resolve().parents[3] / "config"
LATER = datetime(2027, 11, 3, 8, 15, tzinfo=UTC)
RECORDING_DATE = re.compile(r"2026-09-2\d")


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


def replay(settings: Settings, scenario: str, display_end: datetime | None = None) -> Investigation:
    source = load_replay(settings, scenario, display_end=display_end)
    orchestrator = Orchestrator(settings, replay=source)
    return asyncio.run(orchestrator.investigate(InvestigationRequest(question="", mode="replay")))


def with_rules(settings: Settings, **rules: object) -> Settings:
    severity = SeverityRules.model_validate(rules)
    orchestrator = settings.orchestrator.model_copy(update={"severity": severity})
    return settings.model_copy(update={"orchestrator": orchestrator})


# --------------------------------------------------------------------------- severity


@pytest.mark.parametrize(
    ("scenario", "expected"),
    [("S0", "none"), ("S1", "critical"), ("S2", "high"), ("S4", "high"), ("S5", "high")],
)
def test_severity_follows_the_configured_rules(
    settings: Settings, scenario: str, expected: str
) -> None:
    inv = replay(settings, scenario)
    assert inv.report is not None and inv.report.severity == expected


def test_tier_1_without_measured_errors_is_not_critical(settings: Settings) -> None:
    # S5: payment-service (tier 1) is slow because redis is down, but its measured 5xx
    # ratio stays at 0%: high, not critical.
    inv = replay(settings, "S5")
    assert inv.context is not None and inv.context.service == "payment-service"
    assert inv.report is not None and inv.report.severity == "high"
    # With a threshold of 0, any measured ratio counts: now it is critical.
    inv = replay(with_rules(settings, critical_error_rate=0.0), "S5")
    assert inv.report is not None and inv.report.severity == "critical"


def test_rules_come_from_config(settings: Settings) -> None:
    inv = replay(with_rules(settings, critical_tiers=[]), "S1")
    assert inv.report is not None and inv.report.severity == "high"
    # S3: order-service (tier 2) answers 100% 5xx; critical once tier 2 counts
    inv = replay(with_rules(settings, critical_tiers=[1, 2]), "S3")
    assert inv.report is not None and inv.report.severity == "critical"


def test_profile_sets_payment_service_tier_1(settings: Settings) -> None:
    from aiops.core.catalog import ServiceCatalog

    catalog = ServiceCatalog.from_settings(settings)
    tiers = {s.name: s.tier for s in catalog.services}
    assert tiers["payment-service"] == 1
    assert tiers["order-service"] is None  # default_tier


# --------------------------------------------------------------------------- replay clock


def texts(inv: Investigation) -> list[str]:
    """What the UI shows as text. Deep links keep the recorded window on purpose: that's
    where the data is in the source system."""
    assert inv.report is not None
    links = [e.link for e in inv.evidence if e.link]

    def unlinked(text: str) -> str:
        for link in links:
            text = text.replace(link, "")
        return text

    return [
        unlinked(t)
        for t in (
            *(e.summary for e in inv.evidence),
            *(f.description for r in inv.results for f in r.findings),
            *(r.summary for r in inv.results),
            *(t.description for t in inv.timeline),
            *(h.statement for h in inv.hypotheses),
            inv.report.summary,
            inv.report.impact,
            inv.report.markdown,
        )
    ]


@pytest.mark.parametrize("scenario", ["S1", "S3", "S4"])
def test_replay_reported_on_another_clock_moves_every_timestamp(
    settings: Settings, scenario: str
) -> None:
    recorded = replay(settings, scenario)
    shown = replay(settings, scenario, display_end=LATER)
    assert recorded.context is not None and shown.context is not None
    assert recorded.report is not None and shown.report is not None
    delta = LATER - recorded.context.time_range.end
    # the window ends on the reporting clock ...
    assert shown.context.time_range.end == LATER
    # ... and the investigation is otherwise identical, moved by the same delta
    assert [(e.kind, e.source) for e in shown.evidence] == [
        (e.kind, e.source) for e in recorded.evidence
    ]
    assert [t.timestamp for t in shown.timeline] == [t.timestamp + delta for t in recorded.timeline]
    assert [t.description for t in shown.timeline] == [
        shift_text(t.description, delta) for t in recorded.timeline
    ]
    assert [h.statement for h in shown.hypotheses] == [
        shift_text(h.statement, delta) for h in recorded.hypotheses
    ]
    assert shown.report.severity == recorded.report.severity
    assert shown.report.confidence == recorded.report.confidence
    # no recording date anywhere in what the UI shows
    leaked = [
        t[max(0, m.start() - 90) : m.end() + 30]
        for t in texts(shown)
        for m in RECORDING_DATE.finditer(t)
    ]
    assert not leaked, leaked


def test_default_replay_keeps_the_recording_clock(settings: Settings) -> None:
    inv = replay(settings, "S1")
    assert inv.context is not None
    assert inv.context.time_range.end == datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


def test_replay_clock_is_now_on_a_whole_minute() -> None:
    now = datetime(2026, 9, 29, 14, 7, 42, 123456, tzinfo=UTC)
    assert replay_clock(now) == datetime(2026, 9, 29, 14, 7, tzinfo=UTC)
