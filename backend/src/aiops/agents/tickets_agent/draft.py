"""Draft a ticket from investigation findings (UC-04). Drafting never writes anything:
the result becomes an ActionProposal that a human approves (core/guardrails/approvals.py).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from aiops.core.models import AgentResult, AgentStatus, Investigation
from aiops.mcp import tickets

MAX_SUMMARY = 120
MAX_DESCRIPTION = 30_000
_SENTENCE = re.compile(r"(?<=[.!?])\s|\n")


class TicketDraft(BaseModel):
    summary: str
    description: str
    issue_type: str = "Bug"
    priority: str = "Medium"
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    investigation_id: str | None = None
    service: str | None = None

    def create_arguments(self, project_key: str) -> dict[str, Any]:
        """Arguments for ``jira_create_issue`` (mcp-atlassian shapes: JSON strings)."""
        extra = {"labels": self.labels, "priority": {"name": self.priority}}
        arguments: dict[str, Any] = {
            "project_key": project_key,
            "summary": self.summary,
            "issue_type": self.issue_type,
            "description": self.description,
            "additional_fields": json.dumps(extra),
        }
        if self.components:
            arguments["components"] = ",".join(self.components)
        return arguments

    def comment_arguments(self, issue_key: str) -> dict[str, Any]:
        """Arguments for ``jira_add_comment`` on an existing ticket."""
        return {"issue_key": issue_key, "body": f"**{self.summary}**\n\n{self.description}"}


def load_results(payload: Any) -> tuple[list[AgentResult], str | None, str | None]:
    """Accept an AgentResult, a list of them, or an Investigation (JSON-decoded).

    Returns (results, investigation_id, service).
    """
    if isinstance(payload, list):
        return [AgentResult.model_validate(r) for r in payload], None, None
    if isinstance(payload, dict) and "incident" in payload:
        inv = Investigation.model_validate(payload)
        service = (inv.context.service if inv.context else None) or inv.incident.service
        return inv.results, inv.id, service
    if isinstance(payload, dict):
        return [AgentResult.model_validate(payload)], None, None
    raise ValueError("expected an AgentResult, a list of AgentResults or an Investigation")


def _first_sentence(text: str) -> str:
    return _SENTENCE.split(text.strip(), maxsplit=1)[0].strip()


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def draft_ticket(
    results: Sequence[AgentResult],
    *,
    service: str | None,
    labels: Sequence[str] = (),
    components: Sequence[str] = (),
    investigation_id: str | None = None,
    issue_type: str = "Bug",
) -> TicketDraft:
    """Title, description (summary, findings, signals, evidence links) and labels."""
    if not results:
        raise ValueError("no agent results to draft a ticket from")
    relevant = [r for r in results if r.status is AgentStatus.SUCCESS] or list(results)
    lead = relevant[0]
    headline = _first_sentence(lead.summary) or f"{lead.agent} findings"
    prefix = f"[{service}] " if service else ""
    summary = f"{prefix}{headline}"
    if len(summary) > MAX_SUMMARY:
        summary = summary[: MAX_SUMMARY - 1].rstrip() + "…"

    lines = [
        "_Drafted by AI Incident Agents from investigation findings; reviewed and approved by a "
        "human before creation._",
        "",
    ]
    if investigation_id:
        lines += [f"Investigation: `{investigation_id}`", ""]
    lines += ["## Summary"]
    lines += [f"- **{r.agent}** ({r.status.value}): {r.summary.strip()}" for r in relevant]
    findings = [(r.agent, f) for r in relevant for f in r.findings]
    if findings:
        lines += ["", "## Findings"]
        lines += [
            f"- **{f.kind.value}** ({agent}) {f.description}"
            + (f" [{', '.join(f.evidence_ids)}]" if f.evidence_ids else "")
            for agent, f in findings
        ]
    signals = [(r.agent, r.signals) for r in relevant if r.signals]
    if signals:
        lines += ["", "## Signals"]
        lines += [f"- {agent}: {', '.join(s)}" for agent, s in signals]
    evidence = [e for r in relevant for e in r.evidence]
    if evidence:
        lines += ["", "## Evidence", "| Id | Source | Summary | Link |", "|---|---|---|---|"]
        lines += [
            f"| {e.id} | {e.source} | {_cell(e.summary)} | {e.link or ''} |" for e in evidence
        ]
    followups = [f for r in relevant for f in r.suggested_followups]
    if followups:
        lines += ["", "## Suggested follow-ups"]
        lines += [f"- {f}" for f in followups]
    description = "\n".join(lines)
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 20] + "\n…(truncated)"

    strong = any(r.status is AgentStatus.SUCCESS for r in results)
    return TicketDraft(
        summary=summary,
        description=description,
        issue_type=issue_type,
        priority="High" if strong else "Medium",
        labels=list(dict.fromkeys(["aiops", *labels])),
        components=list(components),
        investigation_id=investigation_id,
        service=service,
    )


#: tool -> action name used in proposals
ACTIONS = {tickets.CREATE_ISSUE: "create_ticket", tickets.ADD_COMMENT: "comment_ticket"}
