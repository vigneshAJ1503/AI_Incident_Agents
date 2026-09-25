"""Code agent on real git-mcp responses recorded from the local stack (S0-S5).

Zero tokens: the fake LLM only submits; everything scored here comes from the
agent's deterministic investigation + guardrails, against scenario ground truth.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aiops.agents.code_agent import CodeAgent
from aiops.agents.deps import build_deps
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, EvidenceKind
from aiops.evals.replay import echo_responder
from aiops.evals.runner import run_eval
from aiops.evals.scoring import score_agent
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.scenario_context import SCENARIOS, task_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "code"
CONFIG = Path(__file__).resolve().parents[3] / "config"


def run_agent(scenario: str, llm: FakeLLMProvider, **context: object) -> AgentResult:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / scenario)
    task = task_for(scenario, "code")
    if context:
        task = task.model_copy(update={"context": task.context.model_copy(update=context)})
    return asyncio.run(CodeAgent(deps).run(task))


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_scenario_ground_truth(scenario: str) -> None:
    """Even an LLM that says 'no_signal' can't hide suspects; S0/S5 stay clean."""
    result = run_agent(scenario, FakeLLMProvider(responder=echo_responder("no_signal")))
    card = score_agent(scenario, result, SCENARIOS[scenario].agents["code"])
    failed = [f"{c.name}: {c.detail}" for c in card.checks if not c.passed]
    assert card.passed, failed


@pytest.mark.parametrize("scenario", ["S0", "S5"])
def test_llm_cannot_invent_a_suspect(scenario: str) -> None:
    result = run_agent(
        scenario,
        FakeLLMProvider(responder=echo_responder("success", ["risky_config_change"])),
    )
    assert result.status is AgentStatus.NO_SIGNAL
    assert result.signals == ["no_recent_changes"]


def test_s1_identifies_tune_db_pool_and_quotes_the_hunk() -> None:
    llm = FakeLLMProvider(responder=echo_responder("success"))
    result = run_agent("S1", llm)
    assert [c.tool for c in result.tool_calls] == [
        "list_releases",
        "search_commits",
        "get_diff",
        "get_diff",
        "get_diff",
    ]
    assert all(e.kind is EvidenceKind.COMMIT for e in result.evidence)
    diffs = [e for e in result.evidence if e.source == "code.get_diff"]
    top = max(diffs, key=lambda e: e.data["risk"])
    assert top.data["commit"]["subject"] == "tune db pool"
    assert top.data["commit"]["author"] == "Jordan Lee"
    assert top.timestamp is not None and top.timestamp.isoformat() == "2026-09-25T08:20:00+00:00"
    (hunk,) = top.data["hunks"]
    assert hunk["file"] == "services/payment-service/config/app.yaml"
    assert '-  DB_POOL_SIZE: "20"' in hunk["hunk"] and '+  DB_POOL_SIZE: "2"' in hunk["hunk"]
    assert "DB_POOL_SIZE '20' -> '2'" in top.summary and "changed >= 2x" in top.summary

    request = llm.requests[0]
    system, user = request["messages"][0].content, request["messages"][1].content
    assert "Change window scanned: 2026-09-24T10:00:00Z to 2026-09-25T10:30:00Z" in system
    assert "`unreleased_image_tag`" in system  # signal vocabulary rendered
    suspects = user.split("Suspect commits", 1)[1].split("Other commits", 1)[0]
    assert suspects.index("'tune db pool'") < suspects.index("'release payment-service v1.8.2'")
    assert "Released in the scan window: payment-service/v1.8.2" in user
    assert "'docs(payment-service): link the connection pool runbook'" not in suspects
    assert result.prompt_version and result.prompt_version.startswith("code/v1@")


def test_s3_blames_the_dependency_not_the_service() -> None:
    result = run_agent("S3", FakeLLMProvider(responder=echo_responder()))
    diffs = [e for e in result.evidence if e.source == "code.get_diff"]
    top = max(diffs, key=lambda e: e.data["risk"])
    assert top.data["commit"]["service"] == "inventory-service"
    assert "DROP statement" in top.summary
    assert result.signals == ["dependency_service_change"]


def test_s4_unreleased_image_tag() -> None:
    result = run_agent("S4", FakeLLMProvider(responder=echo_responder()))
    top = next(e for e in result.evidence if e.source == "code.get_diff")
    assert "image tag v3.1.4 -> v3.2.0" in top.summary
    assert "no release tag user-service/v3.2.0" in top.summary


def test_replay_scorecard_all_scenarios_no_false_positives() -> None:
    """`make eval AGENT=code` (replay): every scenario passes, S0/S5 aren't false positives."""
    report = asyncio.run(run_eval(load_settings("local", CONFIG), "code"))
    assert [r.scenario for r in report.results] == ["S0", "S1", "S2", "S3", "S4", "S5"]
    assert report.summary.pass_rate == 1.0, [r.failed_checks for r in report.results]
    assert report.summary.false_positives == 0 and report.summary.citation_validity == 1.0


def test_unknown_service_fails_cleanly() -> None:
    result = run_agent("S1", FakeLLMProvider(), service=None)
    assert result.status is AgentStatus.FAILED
    assert "needs a service" in result.summary
    assert result.tool_calls == []


def test_commit_links_from_settings() -> None:
    settings = load_settings("local", CONFIG)
    code = settings.capabilities["code"]
    patched = code.model_copy(
        update={
            "settings": {
                **code.settings,
                "ui_link_template": "https://git.example.com/{repo}/commit/{sha}",
            }
        }
    )
    settings = settings.model_copy(
        update={"capabilities": {**settings.capabilities, "code": patched}}
    )
    deps = build_deps(
        settings, llm=FakeLLMProvider(responder=echo_responder()), replay_dir=FIXTURES / "S1"
    )
    result = asyncio.run(CodeAgent(deps).run(task_for("S1", "code")))
    diffs = [e for e in result.evidence if e.source == "code.get_diff"]
    assert diffs and all(
        e.link and e.link.startswith("https://git.example.com/sample-repo/commit/") for e in diffs
    )
