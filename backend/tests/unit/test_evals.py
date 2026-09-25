from __future__ import annotations

from pathlib import Path

from aiops.core.models import AgentResult, AgentStatus, Evidence, EvidenceKind
from aiops.evals.scenario import AgentExpectation, load_scenarios
from aiops.evals.scoring import score_agent

SCENARIOS = Path(__file__).resolve().parents[3] / "scenarios"


def test_repo_scenarios_load() -> None:
    scenarios = {s.id: s for s in load_scenarios(SCENARIOS)}
    assert {"S0", "S1"} <= set(scenarios)
    assert scenarios["S0"].root_cause is None
    assert scenarios["S1"].agents["logs"].signals == ["db_timeout_errors_up"]


def _result(status: AgentStatus, signals: list[str], summary: str) -> AgentResult:
    evidence = [Evidence(kind=EvidenceKind.LOG, source="logs.search_logs", summary="x")]
    return AgentResult(
        agent="logs",
        task_id="t",
        status=status,
        summary=summary,
        signals=signals,
        evidence=evidence,
    )


def test_scoring_pass_and_fail() -> None:
    expected = AgentExpectation(
        status=[AgentStatus.SUCCESS],
        signals=["db_timeout_errors_up"],
        forbidden_signals=["oom"],
        must_mention=["Connection"],
        min_evidence=1,
    )
    good = score_agent(
        "S1",
        _result(AgentStatus.SUCCESS, ["db_timeout_errors_up"], "DB connection timeouts"),
        expected,
    )
    assert good.passed and good.score == 1.0

    bad = score_agent("S1", _result(AgentStatus.NO_SIGNAL, ["oom"], "all good"), expected)
    assert not bad.passed
    failed = {c.name for c in bad.checks if not c.passed}
    assert failed == {
        "status",
        "signal:db_timeout_errors_up",
        "no_signal:oom",
        "mentions:Connection",
    }
