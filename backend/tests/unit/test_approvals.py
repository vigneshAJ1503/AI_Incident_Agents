"""Human approval framework: lifecycle, policy, audit, stores, executor, and the guarantee
that agents can never call a write tool (UC-04, MASTER_PLAN §16.2)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from aiops.agents.base import AgentDeps
from aiops.agents.tickets_agent import TicketsAgent
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import CapabilityConfig, ConfigError, Settings, load_settings
from aiops.core.guardrails.approvals import (
    ActionStatus,
    ApprovalError,
    ApprovalExecutor,
    ApprovalPolicy,
    ApprovalService,
    JsonFileApprovalStore,
    MemoryApprovalAuditSink,
    MemoryApprovalStore,
)
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.core.prompts import PromptLoader
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.mcp.registry import MCPRegistry
from tests.fixtures.scenario_context import task_for
from tests.unit.agent_helpers import REPO_CONFIG

T0 = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
CREATE_ARGS = {"project_key": "OPS", "summary": "[payment-service] HTTP 500s", "issue_type": "Bug"}


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


class FakeTracker:
    """In-process MCP server with the tickets contract; records every call it receives."""

    def __init__(self, fail_create: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.server = MCPServer("fake-tickets")
        tracker = self

        @self.server.tool()
        def jira_search(jql: str, fields: str = "", limit: int = 10) -> dict[str, Any]:
            """Search."""
            tracker.calls.append(("jira_search", {"jql": jql}))
            return {"total": 0, "start_at": 0, "max_results": limit, "issues": []}

        @self.server.tool()
        def jira_get_issue(issue_key: str) -> dict[str, Any]:
            """Get."""
            tracker.calls.append(("jira_get_issue", {"issue_key": issue_key}))
            return {"key": issue_key}

        @self.server.tool()
        def jira_create_issue(
            project_key: str, summary: str, issue_type: str, description: str | None = None
        ) -> dict[str, Any]:
            """Create."""
            tracker.calls.append(("jira_create_issue", {"summary": summary}))
            if fail_create:
                raise ToolError("Jira says no")
            return {"message": "Issue created successfully", "issue": {"key": "OPS-99"}}

    def writes(self) -> list[str]:
        return [name for name, _ in self.calls if name != "jira_search"]


def settings() -> Settings:
    return load_settings("local", REPO_CONFIG)


def service(s: Settings | None = None, clock: Clock | None = None) -> ApprovalService:
    return ApprovalService(
        MemoryApprovalStore(),
        MemoryApprovalAuditSink(),
        ApprovalPolicy(s or settings()),
        ttl=timedelta(hours=24),
        clock=clock or Clock(),
    )


def propose(svc: ApprovalService, tool: str = "jira_create_issue", **overrides: Any) -> Any:
    fields: dict[str, Any] = {
        "action": "create_ticket",
        "capability": "tickets",
        "tool": tool,
        "arguments": dict(CREATE_ARGS),
        "reason": "S1 findings",
        "requested_by": "bob",
        "investigation_id": "inv-1",
    }
    fields.update(overrides)
    return svc.propose(**fields)


def audit_trail(svc: ApprovalService) -> list[tuple[str | None, str, str]]:
    records = svc.audit.records  # type: ignore[attr-defined]
    return [(r.from_status, r.to_status, r.actor) for r in records]


# --------------------------------------------------------------------------- lifecycle


def test_propose_approve_lifecycle_is_audited() -> None:
    svc = service()
    proposal = propose(svc)
    assert proposal.status is ActionStatus.PENDING
    assert proposal.expires_at == T0 + timedelta(hours=24)
    approved = svc.approve(proposal.id, "alice", "LGTM")
    assert approved.status is ActionStatus.APPROVED and approved.decided_by == "alice"
    assert audit_trail(svc) == [(None, "pending", "bob"), ("pending", "approved", "alice")]
    record = svc.audit.records[-1]  # type: ignore[attr-defined]
    assert record.arguments_sha256 == proposal.arguments_sha256
    assert record.investigation_id == "inv-1" and record.note == "LGTM"
    with pytest.raises(ApprovalError, match="not pending"):
        svc.approve(proposal.id, "alice")
    with pytest.raises(ApprovalError, match="not pending"):
        svc.deny(proposal.id, "carol")


def test_deny_is_final() -> None:
    svc = service()
    proposal = propose(svc)
    denied = svc.deny(proposal.id, "alice", "duplicate of OPS-12")
    assert denied.status is ActionStatus.DENIED and denied.decision_note == "duplicate of OPS-12"
    with pytest.raises(ApprovalError):
        svc.approve(proposal.id, "alice")


def test_approver_identity_required_and_unknown_ids() -> None:
    svc = service()
    proposal = propose(svc)
    with pytest.raises(ApprovalError, match="approver identity"):
        svc.approve(proposal.id, "  ")
    with pytest.raises(ApprovalError, match="No action proposal"):
        svc.approve("act-nope", "alice")


def test_pending_proposals_expire() -> None:
    clock = Clock()
    svc = service(clock=clock)
    proposal = propose(svc)
    clock.now = T0 + timedelta(hours=25)
    with pytest.raises(ApprovalError, match="expired, not pending"):
        svc.approve(proposal.id, "alice")
    assert svc.get(proposal.id).status is ActionStatus.EXPIRED
    assert audit_trail(svc)[-1] == ("pending", "expired", "system")
    assert svc.proposals(ActionStatus.PENDING) == []


@pytest.mark.parametrize(
    ("tool", "overrides", "reason"),
    [
        ("jira_search", {}, "not in capabilities.tickets.write_allowlist"),
        ("jira_delete_issue", {}, "not in capabilities.tickets.write_allowlist"),
        ("jira_create_issue", {"capability": "nope"}, "Capability 'nope' is not configured"),
        (
            "jira_create_issue",
            {"arguments": {**CREATE_ARGS, "project_key": "WEB"}},
            "not the configured project",
        ),
        (
            "jira_add_comment",
            {"arguments": {"issue_key": "WEB-1", "body": "x"}},
            "outside the configured project",
        ),
        ("jira_create_issue", {"reason": " "}, "a reason is required"),
    ],
)
def test_policy_rejections(tool: str, overrides: dict[str, Any], reason: str) -> None:
    svc = service()
    proposal = propose(svc, tool, **overrides)
    assert proposal.status is ActionStatus.REJECTED
    assert proposal.policy_reason and reason in proposal.policy_reason
    assert audit_trail(svc) == [(None, "rejected", "policy")]
    with pytest.raises(ApprovalError):
        svc.approve(proposal.id, "alice")


def test_json_file_store_roundtrip(tmp_path: Path) -> None:
    store = JsonFileApprovalStore(tmp_path / "approvals.json")
    svc = ApprovalService(
        store, MemoryApprovalAuditSink(), ApprovalPolicy(settings()), clock=Clock()
    )
    proposal = propose(svc)
    svc.approve(proposal.id, "alice")
    reloaded = JsonFileApprovalStore(tmp_path / "approvals.json")
    stored = reloaded.get(proposal.id)
    assert stored is not None and stored.status is ActionStatus.APPROVED
    assert [t.to_status for t in stored.history] == [ActionStatus.PENDING, ActionStatus.APPROVED]
    assert [p.id for p in reloaded.list(ActionStatus.APPROVED)] == [proposal.id]
    assert (
        json.loads((tmp_path / "approvals.json").read_text())["proposals"][0]["id"] == proposal.id
    )
    assert not list(tmp_path.glob(".approvals-*.tmp"))


def test_from_settings_uses_configured_paths(tmp_path: Path) -> None:
    s = settings()
    guardrails = s.guardrails.model_copy(
        update={
            "approvals_path": str(tmp_path / "a.json"),
            "approvals_audit_path": str(tmp_path / "a.jsonl"),
        }
    )
    svc = ApprovalService.from_settings(s.model_copy(update={"guardrails": guardrails}))
    propose(svc)
    assert (tmp_path / "a.json").is_file()
    assert json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])["to_status"] == "pending"


# --------------------------------------------------------------------------- executor


def executor(
    svc: ApprovalService, tracker: FakeTracker, s: Settings | None = None
) -> tuple[ApprovalExecutor, MemoryAuditSink]:
    audit = MemoryAuditSink()
    registry = MCPRegistry(s or settings(), audit=audit, overrides={"tickets": tracker.server})
    return ApprovalExecutor(svc, registry), audit


def test_executor_runs_only_approved_proposals() -> None:
    svc, tracker = service(), FakeTracker()
    run, tool_audit = executor(svc, tracker)
    proposal = propose(svc)
    with pytest.raises(ApprovalError, match="only approved proposals"):
        asyncio.run(run.execute(proposal.id, "alice"))
    svc.deny(proposal.id, "alice")
    with pytest.raises(ApprovalError, match="only approved proposals"):
        asyncio.run(run.execute(proposal.id, "alice"))
    assert tracker.calls == []

    proposal = propose(svc)
    svc.approve(proposal.id, "alice")
    done = asyncio.run(run.execute(proposal.id, "alice"))
    assert done.status is ActionStatus.EXECUTED
    assert done.result == {"message": "Issue created successfully", "issue": {"key": "OPS-99"}}
    assert tracker.writes() == ["jira_create_issue"]
    # Tool-call audit shows who executed it; the approval audit shows every transition.
    assert tool_audit.records[-1].tool_call.agent == "approvals:alice"
    assert audit_trail(svc)[-3:] == [
        (None, "pending", "bob"),
        ("pending", "approved", "alice"),
        ("approved", "executed", "alice"),
    ]
    with pytest.raises(ApprovalError):  # executed once, never again
        asyncio.run(run.execute(proposal.id, "alice"))
    assert tracker.writes() == ["jira_create_issue"]


def test_executor_failure_is_recorded() -> None:
    svc, tracker = service(), FakeTracker(fail_create=True)
    run, _ = executor(svc, tracker)
    proposal = propose(svc)
    svc.approve(proposal.id, "alice")
    failed = asyncio.run(run.execute(proposal.id, "alice"))
    assert failed.status is ActionStatus.FAILED and failed.error
    assert "Jira says no" in failed.error


def test_executor_rechecks_policy_before_writing() -> None:
    svc, tracker = service(), FakeTracker()
    proposal = propose(svc)
    svc.approve(proposal.id, "alice")
    # The write tool was removed from the config after approval.
    s = settings()
    tickets_cap = s.capabilities["tickets"].model_copy(update={"write_allowlist": []})
    changed = s.model_copy(update={"capabilities": {**s.capabilities, "tickets": tickets_cap}})
    svc.policy = ApprovalPolicy(changed)
    run, _ = executor(svc, tracker, changed)
    result = asyncio.run(run.execute(proposal.id, "alice"))
    assert result.status is ActionStatus.FAILED and "policy" in (result.error or "")
    assert tracker.calls == []


# --------------------------------------------------------------------------- agents can't write


def test_config_rejects_write_tools_in_the_agent_allowlist() -> None:
    cap = settings().capabilities["tickets"].model_dump()
    cap["tool_allowlist"] = ["jira_search", "jira_create_issue"]
    with pytest.raises(ValueError, match="must never be available to agents"):
        CapabilityConfig.model_validate(cap)
    s = settings()
    for name, capability in s.capabilities.items():
        assert not set(capability.tool_allowlist) & set(capability.write_allowlist), name


def test_agent_can_never_call_a_write_tool(tmp_path: Path) -> None:
    """A (prompt-injected) LLM asks for jira_create_issue: it isn't offered, the call is
    blocked by the toolset, and the server never sees it."""
    tracker = FakeTracker()
    s = settings()
    audit = MemoryAuditSink()
    attempts: list[str] = []

    def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        if not attempts:
            attempts.append("create")
            return tool_call("jira_create_issue", {**CREATE_ARGS, "summary": "pwned"})
        return tool_call("submit", {"status": "no_signal", "summary": "nothing", "confidence": 0.1})

    llm = FakeLLMProvider(responder=responder)
    deps = AgentDeps(
        settings=s,
        llm=llm,
        mcp=MCPRegistry(s, audit=audit, overrides={"tickets": tracker.server}),
        prompts=PromptLoader(REPO_CONFIG / "prompts"),
        catalog=ServiceCatalog.from_settings(s),
    )
    result = asyncio.run(TicketsAgent(deps).run(task_for("S1", "tickets")))

    offered = {t.name for t in llm.requests[0]["tools"] or []}
    assert offered == {"jira_search", "jira_get_issue", "submit"}
    blocked = [c for c in result.tool_calls if c.tool == "jira_create_issue"]
    assert len(blocked) == 1 and blocked[0].status == "blocked"
    assert tracker.writes() == []  # the server never received a write
    assert any(r.tool_call.status == "blocked" for r in audit.records)


def test_write_toolset_exposes_only_write_tools() -> None:
    tracker = FakeTracker()
    registry = MCPRegistry(
        settings(), audit=MemoryAuditSink(), overrides={"tickets": tracker.server}
    )

    async def check() -> None:
        async with registry.write_toolset("tickets", actor="approvals:alice") as toolset:
            assert {t.name for t in await toolset.specs()} == {"jira_create_issue"}
            outcome = await toolset.call("jira_search", {"jql": "project = OPS"})
            assert outcome.tool_call.status == "blocked"

    asyncio.run(check())
    assert tracker.calls == []


def test_unknown_capability_in_settings_is_config_error() -> None:
    with pytest.raises(ConfigError):
        settings().capability("nope")
