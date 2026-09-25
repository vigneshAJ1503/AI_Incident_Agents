"""Live eval path for the Alert agent: seed Alertmanager at the current time, investigate
through alertmanager-mcp.

Requires `make alertmanager-up` + the alertmanager-mcp container. Run with: make test-integration
Zero tokens: the echo LLM replaces the hosted model. Leaves S1's alerts seeded afterwards.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiops.core.config import load_settings
from aiops.evals.replay import echo_responder
from aiops.evals.runner import run_eval
from aiops.llm.fake import FakeLLMProvider
from aiops.seed.alertmanager import seed_scenario_alerts

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "config"


async def test_live_alert_eval_all_scenarios() -> None:
    try:
        report = await run_eval(
            load_settings("local", CONFIG),
            "alerts",
            mode="live",
            llm_factory=lambda: FakeLLMProvider(responder=echo_responder()),
        )
    finally:
        seed_scenario_alerts(
            os.environ.get("ALERTMANAGER_URL", "http://localhost:9093"), "S1", datetime.now(UTC)
        )
    failed = {
        r.scenario: [f"{c.name}: {c.detail}" for c in r.failed_checks]
        for r in report.results
        if not r.passed
    }
    assert not failed, failed
    assert report.summary.scenarios == 6 and report.summary.false_positive_rate == 0.0
