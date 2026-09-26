"""Zero-token replay: a scripted LLM that submits the agent's deterministic overview.

Used by the eval runner (``--mode replay``) and by agent unit tests. Whatever the
fake LLM says, the result is scored on what the agent's deterministic phase and
guardrails produce from recorded MCP fixtures.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from aiops.core.models import AgentTask, TimeRange, TokenUsage
from aiops.evals.scenario import Scenario
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import Responder, tool_call

#: Anchor time of every recorded fixture (tests/fixtures/<agent>/<scenario>/).
REPLAY_NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)

OVERVIEW_HEADING = "## Overview"
_LABELLED_EVIDENCE = re.compile(r"\[(ev-[0-9a-f]+)\] (\w+)")
_ANY_EVIDENCE = re.compile(r"\bev-[0-9a-f]{12}\b")
PREFERRED_EVIDENCE = ("patterns", "volume")


META_FILE = "meta.json"


class ReplayMeta(BaseModel):
    """The task window of fixtures recorded LIVE (e.g. against Minikube with a fault
    injected), which can't be anchored at ``REPLAY_NOW``: ``<fixtures>/meta.json``."""

    scenario: str
    start: datetime
    end: datetime
    incident_start: datetime | None = None
    recorded_at: datetime | None = None
    note: str = ""

    @classmethod
    def load(cls, fixture_dir: Path) -> ReplayMeta | None:
        path = fixture_dir / META_FILE
        if not path.is_file():
            return None
        return cls.model_validate(json.loads(path.read_text()))

    def save(self, fixture_dir: Path) -> Path:
        fixture_dir.mkdir(parents=True, exist_ok=True)
        path = fixture_dir / META_FILE
        path.write_text(self.model_dump_json(indent=2) + "\n")
        return path

    def apply(self, task: AgentTask) -> AgentTask:
        """The recorded window (and incident start, as a hint) on a scenario's task."""
        context = task.context.model_copy(
            update={"time_range": TimeRange(start=self.start, end=self.end)}
        )
        hints = dict(task.hints)
        if self.incident_start is not None:
            hints.setdefault("incident_start", self.incident_start.isoformat())
        return task.model_copy(update={"context": context, "hints": hints})


def replay_task(scenario: Scenario, agent: str, fixture_dir: Path) -> AgentTask:
    """The task to replay fixtures with: the live-recorded window if there is a
    ``meta.json``, otherwise the scenario window ending at ``REPLAY_NOW``."""
    meta = ReplayMeta.load(fixture_dir)
    if meta is None:
        return scenario.task(agent, REPLAY_NOW)
    return meta.apply(scenario.task(agent, meta.end))


def overview(messages: list[ChatMessage]) -> str:
    """The deterministic overview an agent appended to its user prompt ('' if none)."""
    for message in messages:
        if message.role == "user" and message.content and OVERVIEW_HEADING in message.content:
            return message.content.split(OVERVIEW_HEADING, 1)[1]
    return ""


def evidence_ids(messages: list[ChatMessage]) -> dict[str, str]:
    """Labelled evidence ids from the overview, e.g. ``{"patterns": "ev-..."}``."""
    return {name: eid for eid, name in _LABELLED_EVIDENCE.findall(overview(messages))}


def _cited_evidence(messages: list[ChatMessage]) -> str | None:
    labelled = evidence_ids(messages)
    for name in PREFERRED_EVIDENCE:
        if name in labelled:
            return labelled[name]
    if labelled:
        return next(iter(labelled.values()))
    for message in messages:  # agents without an overview: first id in any tool result
        found = _ANY_EVIDENCE.search(message.content or "")
        if found:
            return found.group(0)
    return None


def echo_responder(status: str = "no_signal", signals: list[str] | None = None) -> Responder:
    """Submit the overview as the summary with one finding citing real evidence.

    The default ``no_signal`` is the strictest replay: anomalies must be surfaced by
    the agent's data-derived signals, since the "LLM" claims nothing is wrong.
    """

    def respond(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
        cited = _cited_evidence(messages)
        findings = (
            [
                {
                    "kind": "OBSERVATION",
                    "type": "overview",
                    "description": "Deterministic overview",
                    "evidence_ids": [cited],
                }
            ]
            if cited
            else []
        )
        response = tool_call(
            "submit",
            {
                "status": status,
                "summary": (overview(messages) or "Replay run.")[:1500],
                "findings": findings,
                "signals": signals or [],
                "confidence": 0.5,
            },
        )
        # Replay spends no real tokens; report zero so scorecards don't suggest otherwise.
        return response.model_copy(update={"usage": TokenUsage(calls=1)})

    return respond
