"""Whole-investigation replay over recorded fixtures (zero tokens, deterministic).

Every agent has its own fixtures per scenario (``tests/fixtures/<agent>/<scenario>/``).
Most are anchored at ``REPLAY_NOW``; some were recorded LIVE (Minikube + fault
injection) with their own window in ``meta.json``. A replayed investigation runs each
agent with the window its fixtures were recorded with, then **normalizes** the result
onto the scenario's anchor (incident start = ``REPLAY_NOW`` - 20 min, as in the
synthetic data), so the timeline and the RCA's time alignment see one coherent clock.

Round-2 steps whose queries depend on round-1 hints (knowledge, tickets) are served by
the most similar recorded call (``lenient`` replay); a follow-up on another service has
no fixtures and is skipped (reported as a recoverable ``error`` event).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from aiops.agents.base import AgentDeps
from aiops.agents.deps import build_deps
from aiops.core.config import Settings
from aiops.core.events import EventSink
from aiops.core.models import AgentResult, AgentTask, Investigation, TimeRange
from aiops.evals.replay import REPLAY_NOW, ReplayMeta, echo_responder
from aiops.evals.scenario import Scenario
from aiops.llm.fake import FakeLLMProvider
from aiops.orchestrator.planner import PlanRequest

#: The synthetic scenarios' incident starts 20 minutes before the window end
#: (aiops.seed.logs.INCIDENT_OFFSET); live recordings are aligned onto it.
REPLAY_INCIDENT_OFFSET = timedelta(minutes=20)

_ISO = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})(?P<sep>[T ])(?P<hm>\d{2}:\d{2})(?P<sec>:\d{2}(?P<frac>\.\d+)?)?"
    r"(?P<tz>Z|[+-]\d{2}:\d{2})?"
)


def shift_text(text: str, delta: timedelta) -> str:
    """Move every ISO-like timestamp in ``text`` by ``delta``, keeping its format."""
    if not delta or not text:
        return text

    def _sub(match: re.Match[str]) -> str:
        sec = match.group("sec") or ""
        frac = match.group("frac") or ""
        raw = f"{match.group('date')}T{match.group('hm')}{sec[:3] if sec else ''}"
        try:
            moved = datetime.fromisoformat(raw) + delta
        except ValueError:
            return match.group(0)
        out = moved.strftime("%Y-%m-%d") + match.group("sep") + moved.strftime("%H:%M")
        if sec:
            out += moved.strftime(":%S") + frac
        return out + (match.group("tz") or "")

    return _ISO.sub(_sub, text)


def shift_result(result: AgentResult, delta: timedelta) -> AgentResult:
    """The same result with evidence timestamps and timestamps in texts moved by ``delta``."""
    if not delta:
        return result
    evidence = [
        e.model_copy(
            update={
                "timestamp": e.timestamp + delta if e.timestamp else None,
                "summary": shift_text(e.summary, delta),
            }
        )
        for e in result.evidence
    ]
    findings = [
        f.model_copy(update={"description": shift_text(f.description, delta)})
        for f in result.findings
    ]
    return result.model_copy(
        update={
            "evidence": evidence,
            "findings": findings,
            "summary": shift_text(result.summary, delta),
        }
    )


def shift_investigation(
    inv: Investigation, delta: timedelta, *, run_delta: timedelta | None = None
) -> Investigation:
    """Move a whole investigation in time (demo data: replays re-dated to 'recently').

    ``delta`` moves the incident's *data* (time range, evidence, timeline, texts);
    ``run_delta`` (default ``delta``) moves the *run* itself: when it was created and ran
    (incident/investigation ``created_at``/``completed_at``, steps, tool calls). A replay
    reads fixtures recorded long ago while it runs "now", so the two differ.
    """
    run = delta if run_delta is None else run_delta
    if not delta and not run:
        return inv
    context = inv.context
    if context is not None:
        tr = context.time_range
        context = context.model_copy(
            update={"time_range": TimeRange(start=tr.start + delta, end=tr.end + delta)}
        )
    steps = [
        s.model_copy(
            update={
                "started_at": s.started_at + run if s.started_at else None,
                "finished_at": s.finished_at + run if s.finished_at else None,
            }
        )
        for s in inv.steps
    ]
    results = [
        shift_result(r, delta).model_copy(
            update={
                "tool_calls": [
                    c.model_copy(update={"started_at": c.started_at + run}) for c in r.tool_calls
                ]
            }
        )
        for r in inv.results
    ]
    timeline = [
        t.model_copy(
            update={
                "timestamp": t.timestamp + delta,
                "description": shift_text(t.description, delta),
            }
        )
        for t in inv.timeline
    ]
    hypotheses = [
        h.model_copy(update={"statement": shift_text(h.statement, delta)}) for h in inv.hypotheses
    ]
    report = inv.report
    if report is not None:
        report = report.model_copy(
            update={
                "summary": shift_text(report.summary, delta),
                "impact": shift_text(report.impact, delta),
                "markdown": shift_text(report.markdown, delta),
            }
        )
    incident = inv.incident.model_copy(update={"created_at": inv.incident.created_at + run})
    return inv.model_copy(
        update={
            "incident": incident,
            "context": context,
            "steps": steps,
            "results": results,
            "timeline": timeline,
            "hypotheses": hypotheses,
            "report": report,
            "created_at": inv.created_at + run,
            "completed_at": inv.completed_at + run if inv.completed_at else None,
        }
    )


@dataclass
class ReplaySource:
    """Recorded fixtures of one scenario, for every agent."""

    scenario: Scenario
    fixtures: Path  # <fixtures>/<agent>/<scenario>/<capability>.json
    anchor_end: datetime = REPLAY_NOW
    #: Seconds each recorded tool call waits (Web UI replays; 0 = instant).
    tool_delay_s: float = 0.0

    @property
    def anchor_incident_start(self) -> datetime | None:
        return None if self.scenario.healthy else self.anchor_end - REPLAY_INCIDENT_OFFSET

    def request(self) -> PlanRequest:
        return PlanRequest(
            question=self.scenario.question,
            service=self.scenario.service,
            environment=self.scenario.environment,
            since=self.scenario.window,
            end=self.anchor_end,
        )

    def fixture_dir(self, agent: str, service: str | None) -> Path | None:
        """Fixtures exist for the scenario's own service only."""
        if service != self.scenario.service:
            return None
        path = self.fixtures / agent / self.scenario.id
        return path if path.is_dir() else None

    def prepare(self, task: AgentTask) -> tuple[AgentTask, timedelta]:
        """The task with its recorded window, and the offset that normalizes its result."""
        path = self.fixture_dir(task.agent, task.context.service)
        meta = ReplayMeta.load(path) if path else None
        if meta is None:
            return task, timedelta(0)
        anchor = self.anchor_incident_start
        if anchor is not None and meta.incident_start is not None:
            offset = anchor - meta.incident_start
        else:
            offset = self.anchor_end - meta.end
        return meta.apply(task), offset

    def deps(
        self, settings: Settings, agent: str, service: str | None, events: EventSink
    ) -> AgentDeps:
        return build_deps(
            settings,
            llm=FakeLLMProvider(responder=echo_responder()),
            replay_dir=self.fixture_dir(agent, service),
            replay_lenient=True,
            replay_delay_s=self.tool_delay_s,
            events=events,
        )


class ReplayError(Exception):
    """A replay can't run (unknown scenario, no fixtures). Message is for humans."""


def load_replay(
    settings: Settings, scenario_id: str, anchor_end: datetime = REPLAY_NOW
) -> ReplaySource:
    """The recorded fixtures of scenario ``scenario_id`` (e.g. ``S1``)."""
    from aiops.evals.runner import EvalPaths
    from aiops.evals.scenario import load_scenarios

    paths = EvalPaths.discover(settings.config_dir)
    scenarios = {s.id.upper(): s for s in load_scenarios(paths.scenarios)}
    scenario = scenarios.get(scenario_id.upper())
    if scenario is None:
        known = ", ".join(sorted(scenarios)) or "none"
        raise ReplayError(f"Unknown scenario '{scenario_id}' (known: {known}).")
    if not paths.fixtures.is_dir():
        raise ReplayError(f"No recorded fixtures in {paths.fixtures}.")
    return ReplaySource(scenario=scenario, fixtures=paths.fixtures, anchor_end=anchor_end)
