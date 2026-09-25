"""Alert agent on real alertmanager-mcp responses recorded from the local stack (S0-S5).

Zero tokens: the fake LLM only submits; everything scored here comes from the
agent's deterministic investigation + guardrails, against scenario ground truth.
Fixtures: `uv run python -m tests.fixtures.record_alerts` (alerts seeded at FIXED_NOW).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from aiops.agents.alert_agent import AlertAgent
from aiops.agents.deps import build_deps
from aiops.core.config import load_settings
from aiops.core.models import AgentResult, AgentStatus, AgentTask, ClaimKind
from aiops.evals.replay import echo_responder, evidence_ids, overview
from aiops.evals.runner import EvalReport, run_eval
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.fixtures.scenario_context import task_for

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "alerts"
CONFIG = Path(__file__).resolve().parents[3] / "config"


def run_agent(scenario: str, llm: FakeLLMProvider, task: AgentTask | None = None) -> AgentResult:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / scenario)
    return asyncio.run(AlertAgent(deps).run(task or task_for(scenario, "alerts")))


@pytest.fixture(scope="module")
def replay_report() -> EvalReport:
    """The shared eval runner in replay mode (the same code as `make eval AGENT=alerts`)."""
    with pytest.MonkeyPatch.context() as mp:  # module scope: isolate from a developer .env
        mp.setattr("aiops.core.config.load_dotenv", lambda *a, **k: False)
        settings = load_settings("local", CONFIG)
    return asyncio.run(run_eval(settings, "alerts", mode="replay"))


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_scenario_ground_truth(replay_report: EvalReport, scenario: str) -> None:
    """An LLM that says 'no_signal' can't hide firing alerts; S0 stays clean."""
    result = next(r for r in replay_report.results if r.scenario == scenario)
    assert result.passed, [f"{c.name}: {c.detail}" for c in result.failed_checks]
    assert not result.false_positive
    assert result.tokens == 0 and result.llm_calls == 1


def test_replay_report_aggregates(replay_report: EvalReport) -> None:
    summary = replay_report.summary
    assert summary.pass_rate == 1.0
    assert summary.healthy_scenarios == 1 and summary.false_positive_rate == 0.0
    assert summary.citation_validity == 1.0


def test_s1_details() -> None:
    llm = FakeLLMProvider(responder=echo_responder("success"))
    result = run_agent("S1", llm)
    assert result.status is AgentStatus.SUCCESS
    assert result.signals == ["alerts_firing", "critical_alert_firing"]
    # service + one call per depends_on (postgres, redis, user-service) + silences
    assert [c.tool for c in result.tool_calls] == ["list_alerts"] * 4 + ["list_silences"]
    service_args = result.tool_calls[0].arguments
    assert service_args["labels"] == {"service": "payment-service", "namespace": "prod"}
    assert result.tool_calls[2].arguments["labels"] == {"service": "redis"}  # not in catalog

    service_ev = result.evidence[0]
    assert service_ev.summary == (
        "2 firing alert(s) for payment-service: DatabaseConnectionPoolExhausted "
        "(critical, since 2026-09-25T10:11:00Z); HighErrorRate (critical, since 2026-09-25T10:12:00Z)"
    )
    assert service_ev.kind.value == "alert"
    assert service_ev.timestamp is not None and service_ev.timestamp.isoformat().startswith(
        "2026-09-25T10:11"
    )
    assert service_ev.link is not None and service_ev.link.startswith(
        "http://localhost:9093/#/alerts?"
    )
    assert "service%3D%22payment-service%22" in service_ev.link
    assert [e.summary for e in result.evidence[1:4]] == [
        "No active alerts for postgres",
        "No active alerts for redis",
        "No active alerts for user-service",
    ]

    request = llm.requests[0]
    system, user = request["messages"][0].content, request["messages"][1].content
    assert "Incident window (UTC): 2026-09-25T10:00:00Z to 2026-09-25T10:30:00Z" in system
    assert '{"namespace": "prod", "service": "payment-service"}' in system
    assert "`dependency_alert_firing`" in system  # signal vocabulary rendered
    assert (
        "[critical] DatabaseConnectionPoolExhausted on payment-service since "
        "2026-09-25T10:11:00Z (+11m vs incident start, inside the window)"
    ) in user
    assert "runbook: knowledge-base/runbooks/database-connection-pool.md" in user
    assert "First alert to fire: DatabaseConnectionPoolExhausted" in user
    assert "never present an alert as the root cause" in user
    assert result.prompt_version and result.prompt_version.startswith("alerts/v1@")


def respond_with(
    status: str, signals: list[str], summary: str, kind: str = "OBSERVATION", type_: str = "alert"
):  # type: ignore[no-untyped-def]
    def respond(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        cited = evidence_ids(messages)["service_alerts"]
        return tool_call(
            "submit",
            {
                "status": status,
                "summary": summary,
                "findings": [
                    {"kind": kind, "type": type_, "description": summary, "evidence_ids": [cited]}
                ],
                "signals": signals,
                "confidence": 0.9,
            },
        )

    return respond


def test_s0_llm_cannot_invent_alerts_and_all_clear_is_explicit() -> None:
    llm = FakeLLMProvider(
        responder=respond_with(
            "success", ["alerts_firing", "critical_alert_firing"], "HighErrorRate is firing."
        )
    )
    result = run_agent("S0", llm)
    assert result.status is AgentStatus.NO_SIGNAL
    assert result.signals == ["no_active_alerts"]
    assert result.summary.startswith(
        "No active alerts for payment-service or its dependencies (postgres, redis, user-service)"
    )


def test_alert_is_never_a_root_cause_fact() -> None:
    llm = FakeLLMProvider(
        responder=respond_with(
            "success", [], "The pool alert is the root cause.", kind="FACT", type_="root_cause"
        )
    )
    result = run_agent("S1", llm)
    assert [f.kind for f in result.findings] == [ClaimKind.HYPOTHESIS]
    assert result.summary == "The pool alert is the root cause."  # alerts fire: no prefix


def test_s3_dependency_alert_is_attributed_to_the_dependency() -> None:
    llm = FakeLLMProvider(responder=echo_responder())
    result = run_agent("S3", llm)
    text = overview(llm.requests[0]["messages"])
    assert "Firing alerts on dependencies (1):" in text
    assert "[warning] HighLatencyP95 on inventory-service since 2026-09-25T10:15:00Z" in text
    assert "First alert to fire: HighErrorRate on order-service" in text
    assert "dependency_alert_firing" in result.signals


def test_incident_start_hint_moves_the_correlation_anchor() -> None:
    task = task_for("S1", "alerts")
    task = task.model_copy(update={"hints": {"incident_start": "2026-09-25T10:15:00Z"}})
    llm = FakeLLMProvider(responder=echo_responder())
    result = run_agent("S1", llm, task)
    assert "alert_precedes_incident" in result.signals
    text = overview(llm.requests[0]["messages"])
    assert "2026-09-25T10:15:00Z (hint 'incident_start')" in text
    assert "(-4m vs incident start, inside the window)" in text


def test_hint_outside_the_window_is_ignored() -> None:
    task = task_for("S1", "alerts")
    task = task.model_copy(update={"hints": {"incident_start": "2026-09-24T10:15:00Z"}})
    result = run_agent("S1", FakeLLMProvider(responder=echo_responder()), task)
    assert "alert_precedes_incident" not in result.signals


def test_unknown_service_fails_cleanly() -> None:
    task = task_for("S1", "alerts")
    no_service = task.model_copy(
        update={"context": task.context.model_copy(update={"service": None})}
    )
    result = run_agent("S1", FakeLLMProvider(), no_service)
    assert result.status is AgentStatus.FAILED and "needs a catalog service" in result.summary
    assert result.tool_calls == []


def test_mcp_failure_is_reported_not_hidden(tmp_path: Path) -> None:
    import json

    fixture: dict[str, Any] = json.loads((FIXTURES / "S1" / "alerts.json").read_text())
    for call in fixture["calls"]:
        if call["tool"] == "list_alerts":
            call["result"]["is_error"] = True
            call["result"]["structured"] = None
            call["result"]["text"] = "Alertmanager unreachable: connection refused"
    (tmp_path / "S1").mkdir()
    (tmp_path / "S1" / "alerts.json").write_text(json.dumps(fixture))
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=FakeLLMProvider(), replay_dir=tmp_path / "S1")
    result = asyncio.run(AlertAgent(deps).run(task_for("S1", "alerts")))
    assert result.status is AgentStatus.FAILED
    assert "Could not query alerts" in result.summary and "unreachable" in result.summary
    assert result.signals == []  # no "no_active_alerts" when we couldn't look
