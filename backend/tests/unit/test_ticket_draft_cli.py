"""`aiops tickets draft` + `aiops approvals ...` end to end, with an in-process tracker."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aiops.agents.tickets_agent.draft import draft_ticket, load_results
from aiops.cli.main import app
from aiops.core.config import Settings
from aiops.core.guardrails.approvals import ActionStatus, JsonFileApprovalStore
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    ClaimKind,
    Evidence,
    EvidenceKind,
    Finding,
    Incident,
    IncidentContext,
    Investigation,
    TimeRange,
)
from aiops.mcp.registry import MCPRegistry
from tests.unit.agent_helpers import NOW
from tests.unit.test_approvals import FakeTracker, settings

runner = CliRunner()


def result(status: AgentStatus = AgentStatus.SUCCESS) -> AgentResult:
    evidence = Evidence(
        kind=EvidenceKind.TICKET,
        source="tickets.jira_search",
        summary="OPS-12 [Open] payment-service DB connection timeouts | pipes",
        link="http://localhost:8109/browse/OPS-12",
    )
    return AgentResult(
        agent="tickets",
        task_id="task-1",
        status=status,
        summary="Known issue OPS-12 matches the HTTP 500s. It is open since 3 days.",
        findings=[
            Finding(
                kind=ClaimKind.FACT,
                type="known_issue",
                description="OPS-12 is open",
                evidence_ids=[evidence.id],
            )
        ],
        evidence=[evidence],
        signals=["known_issue_open"],
        suggested_followups=["Logs agent: compare messages"],
    )


def test_draft_ticket_content() -> None:
    draft = draft_ticket(
        [result()],
        service="payment-service",
        labels=["payment-service"],
        components=["payments"],
        investigation_id="inv-1",
    )
    assert draft.summary == "[payment-service] Known issue OPS-12 matches the HTTP 500s."
    assert draft.priority == "High" and draft.labels == ["aiops", "payment-service"]
    for part in (
        "Investigation: `inv-1`",
        "## Findings",
        "**FACT** (tickets) OPS-12 is open [ev-",
        "## Signals\n- tickets: known_issue_open",
        "| http://localhost:8109/browse/OPS-12 |",
        "timeouts \\| pipes",  # table cells escaped
        "## Suggested follow-ups",
    ):
        assert part in draft.description
    args = draft.create_arguments("OPS")
    assert args["project_key"] == "OPS" and args["components"] == "payments"
    assert json.loads(args["additional_fields"]) == {
        "labels": ["aiops", "payment-service"],
        "priority": {"name": "High"},
    }
    comment = draft.comment_arguments("OPS-12")
    assert comment["issue_key"] == "OPS-12" and comment["body"].startswith("**[payment-service]")


def test_draft_prefers_successful_results_and_truncates_title() -> None:
    quiet = result(AgentStatus.NO_SIGNAL).model_copy(update={"agent": "logs"})
    loud = result().model_copy(update={"summary": "x" * 300})
    draft = draft_ticket([quiet, loud], service=None)
    assert len(draft.summary) == 120 and draft.summary.endswith("…")
    assert "**logs**" not in draft.description
    with pytest.raises(ValueError):
        draft_ticket([], service=None)


def test_load_results_formats() -> None:
    one = result()
    assert load_results(one.model_dump(mode="json"))[0][0].agent == "tickets"
    assert len(load_results([one.model_dump(mode="json")] * 2)[0]) == 2
    context = IncidentContext(
        question="q", service="payment-service", time_range=TimeRange.last("30m", now=NOW)
    )
    inv = Investigation(incident=Incident(title="t"), context=context, results=[one])
    results, inv_id, service = load_results(inv.model_dump(mode="json"))
    assert inv_id == inv.id and service == "payment-service" and len(results) == 1
    with pytest.raises(ValueError):
        load_results("nope")


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeTracker, Settings]:
    """Settings with approval files in tmp_path and the tickets MCP served in-process."""
    base = settings()
    guardrails = base.guardrails.model_copy(
        update={
            "approvals_path": str(tmp_path / "approvals.json"),
            "approvals_audit_path": str(tmp_path / "approvals-audit.jsonl"),
        }
    )
    s = base.model_copy(update={"guardrails": guardrails})
    tracker = FakeTracker()

    def load(env: str | None = None) -> Settings:
        return s

    def registry(settings: Settings, **_: Any) -> MCPRegistry:
        return MCPRegistry(settings, audit=MemoryAuditSink(), overrides={"tickets": tracker.server})

    monkeypatch.setattr("aiops.cli.tickets_cmd.load_settings", load)
    monkeypatch.setattr("aiops.cli.approvals_cmd.load_settings", load)
    monkeypatch.setattr("aiops.cli.approvals_cmd.MCPRegistry", registry)
    return tracker, s


def test_draft_then_approve_executes_once(
    tmp_path: Path, cli_env: tuple[FakeTracker, Settings]
) -> None:
    tracker, _ = cli_env
    findings = tmp_path / "result.json"
    findings.write_text(result().model_dump_json())

    out = runner.invoke(app, ["tickets", "draft", "--from-result", str(findings), "-s", "payments"])
    assert out.exit_code == 0, out.output
    assert "Nothing has been written" in out.output
    store = JsonFileApprovalStore(tmp_path / "approvals.json")
    [proposal] = store.list()
    assert proposal.status is ActionStatus.PENDING and proposal.tool == "jira_create_issue"
    assert proposal.arguments["components"] == "payments"
    assert tracker.writes() == []  # drafting never writes

    listed = runner.invoke(app, ["approvals", "list", "--status", "pending", "--json"])
    assert proposal.id in listed.output

    approved = runner.invoke(app, ["approvals", "approve", proposal.id, "--by", "alice"])
    assert approved.exit_code == 0, approved.output
    assert tracker.writes() == ["jira_create_issue"]
    stored = store.get(proposal.id)
    assert stored is not None and stored.status is ActionStatus.EXECUTED
    assert stored.decided_by == "alice"

    again = runner.invoke(app, ["approvals", "approve", proposal.id, "--by", "alice"])
    assert again.exit_code == 1 and "not pending" in again.output
    assert tracker.writes() == ["jira_create_issue"]

    shown = runner.invoke(app, ["approvals", "show", proposal.id])
    assert "executed" in shown.output and "alice" in shown.output
    audit = [
        json.loads(line) for line in (tmp_path / "approvals-audit.jsonl").read_text().splitlines()
    ]
    assert [(a["to_status"], a["actor"]) for a in audit] == [
        ("pending", audit[0]["actor"]),
        ("approved", "alice"),
        ("executed", "alice"),
    ]


def test_deny_and_comment_proposals(tmp_path: Path, cli_env: tuple[FakeTracker, Settings]) -> None:
    tracker, _ = cli_env
    findings = tmp_path / "result.json"
    findings.write_text(result().model_dump_json())
    out = runner.invoke(app, ["tickets", "draft", "-f", str(findings), "--comment-on", "OPS-12"])
    assert out.exit_code == 0, out.output
    [proposal] = JsonFileApprovalStore(tmp_path / "approvals.json").list()
    assert proposal.tool == "jira_add_comment" and proposal.risk == "low"
    denied = runner.invoke(
        app, ["approvals", "deny", proposal.id, "--by", "alice", "--reason", "dup"]
    )
    assert denied.exit_code == 0 and "denied" in denied.output
    execute = runner.invoke(app, ["approvals", "execute", proposal.id])
    assert execute.exit_code == 1 and "only approved proposals" in execute.output
    assert tracker.calls == []


def test_draft_rejected_by_policy(tmp_path: Path, cli_env: tuple[FakeTracker, Settings]) -> None:
    findings = tmp_path / "result.json"
    findings.write_text(result().model_dump_json())
    out = runner.invoke(app, ["tickets", "draft", "-f", str(findings), "--comment-on", "WEB-1"])
    assert out.exit_code == 1 and "outside the configured project" in out.output


def test_draft_needs_exactly_one_source(cli_env: tuple[FakeTracker, Settings]) -> None:
    out = runner.invoke(app, ["tickets", "draft"])
    assert out.exit_code != 0
    missing = runner.invoke(app, ["tickets", "draft", "--investigation", "inv-missing"])
    assert missing.exit_code == 1 and "Not found" in missing.output
