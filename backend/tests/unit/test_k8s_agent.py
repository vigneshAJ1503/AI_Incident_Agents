"""K8s agent on kubernetes-mcp responses recorded LIVE from Minikube (S0-S5).

Zero tokens: the fake LLM only submits; everything scored here comes from the
agent's deterministic investigation + guardrails, against scenario ground truth.
Fixtures were recorded with `aiops fault run S<n> -- python -m tests.fixtures.record_k8s`
(S0 without a fault); each has a meta.json with the live window it was recorded with.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from aiops.agents.deps import build_deps
from aiops.agents.k8s_agent import K8sAgent
from aiops.agents.k8s_agent.analysis import SIGNALS
from aiops.core.config import load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.core.models import AgentResult, AgentStatus, AgentTask, ClaimKind
from aiops.evals.replay import (
    REPLAY_NOW,
    ReplayMeta,
    echo_responder,
    evidence_ids,
    replay_task,
)
from aiops.evals.runner import EvalReport, run_eval
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.mcp.registry import MCPRegistry
from tests.fixtures.scenario_context import SCENARIOS

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "k8s"
CONFIG = Path(__file__).resolve().parents[3] / "config"
PROMPT = CONFIG / "prompts" / "k8s" / "v1.md"


def task(scenario: str) -> AgentTask:
    return replay_task(SCENARIOS[scenario], "k8s", FIXTURES / scenario)


def run_agent(scenario: str, llm: FakeLLMProvider) -> AgentResult:
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=llm, replay_dir=FIXTURES / scenario)
    return asyncio.run(K8sAgent(deps).run(task(scenario)))


@pytest.fixture(scope="module")
def replay_report() -> EvalReport:
    """The shared eval runner in replay mode (the same code as `make eval AGENT=k8s`)."""
    with pytest.MonkeyPatch.context() as mp:  # module scope: isolate from a developer .env
        mp.setattr("aiops.core.config.load_dotenv", lambda *a, **k: False)
        settings = load_settings("local", CONFIG)
    return asyncio.run(run_eval(settings, "k8s", mode="replay"))


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_scenario_ground_truth(replay_report: EvalReport, scenario: str) -> None:
    """An LLM that says 'no_signal' can't hide a broken workload; S0 stays clean."""
    result = next(r for r in replay_report.results if r.scenario == scenario)
    assert result.passed, [f"{c.name}: {c.detail}" for c in result.failed_checks]
    assert not result.false_positive
    assert result.tokens == 0 and result.llm_calls == 1


def test_replay_report_aggregates(replay_report: EvalReport) -> None:
    summary = replay_report.summary
    assert summary.scenarios == 6 and summary.pass_rate == 1.0
    assert summary.healthy_scenarios == 1 and summary.false_positive_rate == 0.0
    assert summary.citation_validity == 1.0


@pytest.mark.parametrize("scenario", ["S0", "S1", "S2", "S3", "S4", "S5"])
def test_fixtures_are_live_recordings_with_their_window(scenario: str) -> None:
    meta = ReplayMeta.load(FIXTURES / scenario)
    assert meta is not None and meta.scenario == scenario
    assert meta.start < meta.end
    if scenario == "S0":
        assert meta.incident_start is None
    else:
        assert meta.incident_start is not None and meta.start <= meta.incident_start <= meta.end
        assert task(scenario).hints["incident_start"] == meta.incident_start.isoformat()


def test_deterministic_calls_and_scope() -> None:
    llm = FakeLLMProvider(responder=echo_responder())
    result = run_agent("S1", llm)
    assert [c.tool for c in result.tool_calls] == [
        "get_deployment",
        "list_pods",
        "list_events",
        "list_deployments",
    ]
    args = [c.arguments for c in result.tool_calls]
    assert args[0] == {"namespace": "prod", "name": "payment-service", "history": 5}
    assert args[1]["label_selector"] == "app=payment-service"  # from the service catalog
    assert args[2]["object_name_prefix"] == "payment-service" and args[2]["type"] == "Warning"
    meta = ReplayMeta.load(FIXTURES / "S1")
    assert meta is not None and args[2]["since"] == meta.start.isoformat().replace("+00:00", "Z")
    assert all(e.kind.value == "k8s_event" for e in result.evidence)

    request = llm.requests[0]
    system, user = request["messages"][0].content, request["messages"][1].content
    assert "deployment `payment-service` in namespace `prod`" in system
    assert "Dependencies (from the service catalog): postgres, redis, user-service" in system
    assert all(f"`{s}`" in system for s in SIGNALS)  # vocabulary rendered
    assert "## Overview (computed for you, deterministic)" in user
    assert set(evidence_ids(request["messages"])) == {
        "deployment",
        "pods",
        "events",
        "dependencies",
    }
    assert result.prompt_version and result.prompt_version.startswith("k8s/v1@")


def test_signal_vocabulary_matches_the_prompt() -> None:
    text = PROMPT.read_text()
    for signal in SIGNALS:
        assert f"`{signal}`" in text


def fact_responder(signals: list[str], kind: str = "FACT", type_: str = "root_cause"):  # type: ignore[no-untyped-def]
    def respond(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        cited = evidence_ids(messages)["deployment"]
        return tool_call(
            "submit",
            {
                "status": "success",
                "summary": "The rollout broke everything.",
                "findings": [
                    {
                        "kind": kind,
                        "type": type_,
                        "description": "The rollout is the root cause.",
                        "evidence_ids": [cited],
                    }
                ],
                "signals": signals,
                "confidence": 0.9,
            },
        )

    return respond


def test_llm_cannot_invent_problems_on_a_healthy_cluster() -> None:
    result = run_agent("S0", FakeLLMProvider(responder=fact_responder(["oom_killed"])))
    assert result.signals == ["healthy"]
    assert result.status is AgentStatus.NO_SIGNAL
    root = next(f for f in result.findings if f.type == "root_cause")
    assert root.kind is ClaimKind.HYPOTHESIS  # never a FACT
    assert result.summary.startswith("payment-service: 1/1 replicas ready")


def test_rollout_is_reported_but_never_a_fact_cause() -> None:
    result = run_agent("S1", FakeLLMProvider(responder=fact_responder([])))
    assert result.status is AgentStatus.SUCCESS and "recent_rollout" in result.signals
    kinds = {f.type: f.kind for f in result.findings}
    assert kinds["root_cause"] is ClaimKind.HYPOTHESIS
    assert kinds["recent_rollout"] is ClaimKind.FACT  # the rollout itself is a fact
    known = {e.id for e in result.evidence}
    assert all(set(f.evidence_ids) <= known and f.evidence_ids for f in result.findings)


def failing_server(fail: set[str]) -> MCPServer:
    server = MCPServer("fake-k8s")

    def boom(tool: str) -> None:
        if tool in fail:
            raise ToolError(f"{tool}: Kubernetes API unreachable")

    @server.tool()
    def get_deployment(namespace: str, name: str, history: int = 5) -> dict[str, Any]:
        """Deployment."""
        boom("get_deployment")
        return {
            "deployment": {
                "name": name,
                "replicas": {"desired": 1, "ready": 1, "available": 1, "unavailable": 0},
                "revision": "3",
                "images": [{"container": "app", "image": "img:1"}],
                "conditions": [],
            },
            "rollout_history": [],
        }

    @server.tool()
    def list_pods(
        namespace: str, label_selector: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Pods."""
        boom("list_pods")
        return {"pods": []}

    @server.tool()
    def list_events(
        namespace: str,
        object_name_prefix: str | None = None,
        type: str = "all",
        since: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Events."""
        boom("list_events")
        return {"events": []}

    @server.tool()
    def list_deployments(namespace: str, limit: int = 50) -> dict[str, Any]:
        """Deployments."""
        boom("list_deployments")
        return {"deployments": []}

    return server


def run_with_server(server: MCPServer) -> AgentResult:
    settings = load_settings("local", CONFIG)
    registry = MCPRegistry(settings, audit=MemoryAuditSink(), overrides={"k8s": server})
    deps = build_deps(settings, llm=FakeLLMProvider(responder=echo_responder()), mcp=registry)
    return asyncio.run(K8sAgent(deps).run(task("S0")))


def test_everything_down_fails_cleanly() -> None:
    fail = {"get_deployment", "list_pods", "list_events", "list_deployments"}
    result = run_with_server(failing_server(fail))
    assert result.status is AgentStatus.FAILED
    assert result.error and "Could not query Kubernetes" in result.error


def test_partial_data_is_never_called_healthy() -> None:
    result = run_with_server(failing_server({"list_events"}))
    assert result.status is AgentStatus.NO_SIGNAL
    assert "healthy" not in result.signals
    assert "Could not query: events" in result.summary
    # redis/postgres/user-service were not in the fake cluster: reported, not flagged
    assert "dependency_unavailable" not in result.signals


def test_replay_task_uses_meta_window_or_the_fixed_anchor(tmp_path: Path) -> None:
    plain = replay_task(SCENARIOS["S1"], "k8s", tmp_path)  # no meta.json
    assert plain.context.time_range.end == REPLAY_NOW and "incident_start" not in plain.hints

    start, end = datetime(2026, 9, 26, 9, 0, tzinfo=UTC), datetime(2026, 9, 26, 9, 4, tzinfo=UTC)
    incident = datetime(2026, 9, 26, 9, 1, tzinfo=UTC)
    ReplayMeta(scenario="S1", start=start, end=end, incident_start=incident).save(tmp_path)
    live = replay_task(SCENARIOS["S1"], "k8s", tmp_path)
    assert (live.context.time_range.start, live.context.time_range.end) == (start, end)
    assert live.hints["incident_start"] == incident.isoformat()
    assert live.context.service == "payment-service"
