"""Zero-token replay: a scripted LLM that submits the agent's deterministic overview.

Used by the eval runner (``--mode replay``) and by agent unit tests. Whatever the
fake LLM says, the result is scored on what the agent's deterministic phase and
guardrails produce from recorded MCP fixtures.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from aiops.core.models import TokenUsage
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import Responder, tool_call

#: Anchor time of every recorded fixture (tests/fixtures/<agent>/<scenario>/).
REPLAY_NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
#: Fixtures recorded from a LIVE fault (not at REPLAY_NOW) store their window here.
REPLAY_META = "meta.json"


def _ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


@dataclass(frozen=True)
class ReplayMeta:
    """The task window a live fixture was recorded with (``meta.json``)."""

    scenario: str
    start: datetime
    end: datetime
    incident_start: datetime | None = None

    @classmethod
    def load(cls, directory: Path) -> ReplayMeta | None:
        path = directory / REPLAY_META
        if not path.is_file():
            return None
        data = json.loads(path.read_text())
        incident = data.get("incident_start")
        return cls(
            scenario=str(data["scenario"]),
            start=_ts(data["start"]),
            end=_ts(data["end"]),
            incident_start=_ts(incident) if incident else None,
        )


OVERVIEW_HEADING = "## Overview"
_LABELLED_EVIDENCE = re.compile(r"\[(ev-[0-9a-f]+)\] (\w+)")
_ANY_EVIDENCE = re.compile(r"\bev-[0-9a-f]{12}\b")
PREFERRED_EVIDENCE = ("patterns", "volume")


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
