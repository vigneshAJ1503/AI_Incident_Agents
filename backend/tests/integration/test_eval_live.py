"""Live eval path: seed ES at the current time, investigate through the MCP server.

Requires `make infra-up mcp-up`. Run with: make test-integration
Zero tokens: the echo LLM replaces the hosted model (the key check is skipped when
an LLM factory is passed). Leaves S1 seeded at the current time (the default state).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aiops.core.config import load_settings
from aiops.evals.replay import echo_responder
from aiops.evals.runner import run_eval
from aiops.llm.fake import FakeLLMProvider

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "config"


async def test_live_eval_seeds_and_scores_s0_s1() -> None:
    report = await run_eval(
        load_settings("local", CONFIG),
        "logs",
        mode="live",
        scenario_ids=["S1", "S0"],  # run in id order, so S1 stays seeded afterwards
        llm_factory=lambda: FakeLLMProvider(responder=echo_responder()),
    )
    by_id = {r.scenario: r for r in report.results}
    assert list(by_id) == ["S0", "S1"]
    assert by_id["S0"].passed and not by_id["S0"].false_positive
    assert by_id["S1"].passed, [f"{c.name}: {c.detail}" for c in by_id["S1"].failed_checks]
    assert "db_timeout_errors_up" in by_id["S1"].signals
    assert report.summary.pass_rate == 1.0 and report.summary.false_positive_rate == 0.0
