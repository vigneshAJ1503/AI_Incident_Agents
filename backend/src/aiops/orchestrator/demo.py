"""Demo data (PR-034): real replay investigations + a synthetic 14-day history.

* ``replay_demo``: full investigations over scenarios S0-S5 from recorded fixtures (fake
  LLM, zero tokens), re-dated to the last hours, ``mode: "demo"``.
* ``synthetic_history``: about 40 investigations over 14 days (seeded RNG, deterministic
  for a given ``now``), each cloned from one of the replays (so every field is real
  output of the pipeline) with a new id, date, duration and sometimes a partial/failed
  status. Durations are live-like (1-6 min), since replays take milliseconds.
* ``export_demo``: contract-shaped JSON for the Web UI's static demo dataset.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiops.core.catalog import ServiceCatalog
from aiops.core.config import Settings
from aiops.core.events import EventBus, InvestigationEvent
from aiops.core.models import (
    AgentStatus,
    Investigation,
    InvestigationStatus,
    StepStatus,
    new_id,
)
from aiops.evals.runner import EvalPaths
from aiops.evals.scenario import Scenario, load_scenarios
from aiops.orchestrator.dashboard import agent_stats, dashboard_summary
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import ReplaySource, shift_investigation
from aiops.store.repository import InvestigationStore, summary_of

DEMO_SEED = 42
HISTORY_DAYS = 14
HISTORY_COUNT = 40
#: How often each scenario template appears in the synthetic history.
TEMPLATE_WEIGHTS = {"S0": 5, "S1": 4, "S2": 3, "S3": 3, "S4": 2, "S5": 3}


@dataclass
class DemoData:
    investigations: list[Investigation] = field(default_factory=list)
    events: dict[str, list[InvestigationEvent]] = field(default_factory=dict)
    scenarios: list[Scenario] = field(default_factory=list)


def _shift_events(
    events: list[InvestigationEvent], investigation_id: str, delta: timedelta
) -> list[InvestigationEvent]:
    return [
        e.model_copy(
            update={"investigation_id": investigation_id, "timestamp": e.timestamp + delta}
        )
        for e in events
    ]


async def replay_demo(settings: Settings, now: datetime) -> DemoData:
    """S0-S5 replayed end to end, re-dated so the newest finished an hour ago."""
    paths = EvalPaths.discover(settings.config_dir)
    data = DemoData(scenarios=load_scenarios(paths.scenarios))
    for index, scenario in enumerate(sorted(data.scenarios, key=lambda s: s.id)):
        bus = EventBus()
        replay = ReplaySource(scenario=scenario, fixtures=paths.fixtures)
        orchestrator = Orchestrator(settings, replay=replay, bus=bus)
        inv = await orchestrator.investigate(InvestigationRequest(question="", mode="replay"))
        target = now - timedelta(hours=1 + 3 * (len(data.scenarios) - 1 - index))
        data_delta = target - (inv.context.time_range.end if inv.context else inv.created_at)
        run_delta = target - inv.created_at
        # The data (recorded fixtures) and the run (wall clock) move separately, so the
        # run's steps and tool calls stay inside [created_at, completed_at] <= now.
        inv = shift_investigation(inv, data_delta, run_delta=run_delta)
        inv = inv.model_copy(
            update={
                "mode": "demo",
                "created_at": target,
                "completed_at": target + timedelta(milliseconds=inv.duration_ms or 0),
            }
        )
        data.investigations.append(inv)
        data.events[inv.id] = _shift_events(bus.history(inv.id), inv.id, run_delta)
    return data


def _clone(
    template: Investigation, created: datetime, duration_ms: float, rng: random.Random
) -> Investigation:
    delta = created - template.created_at
    inv = shift_investigation(template, delta)
    # Agents take a live-like share of the live-like duration (replays run in milliseconds,
    # which made the Agents page and the dashboard show "p50 2 ms").
    scale = duration_ms / template.duration_ms if template.duration_ms else 1.0
    results = [
        r.model_copy(update={"duration_ms": round(r.duration_ms * scale, 1)}) for r in inv.results
    ]
    return inv.model_copy(
        update={
            "results": results,
            "id": new_id("inv"),
            "incident": inv.incident.model_copy(update={"id": f"INC-{rng.randint(1000, 1999)}"}),
            "created_at": created,
            "completed_at": created + timedelta(milliseconds=duration_ms),
            "duration_ms": duration_ms,
        }
    )


def _degrade(inv: Investigation, status: InvestigationStatus, rng: random.Random) -> Investigation:
    """A partial run (one data source down) or a failed one (every source down)."""
    if status is InvestigationStatus.PARTIAL and inv.results:
        victim = rng.choice([r for r in inv.results])
        results = [
            r.model_copy(
                update={
                    "status": AgentStatus.FAILED,
                    "summary": f"Data source unavailable: {victim.agent} MCP server timed out",
                    "error": "MCP server timed out",
                    "evidence": [],
                    "findings": [],
                    "signals": [],
                }
            )
            if r is victim
            else r
            for r in inv.results
        ]
        steps = [
            s.model_copy(update={"status": StepStatus.FAILED}) if s.id == victim.task_id else s
            for s in inv.steps
        ]
        report = inv.report
        if report is not None:
            report = report.model_copy(
                update={
                    "open_questions": [
                        f"Not checked / incomplete: {victim.agent}: MCP server timed out",
                        *report.open_questions,
                    ]
                }
            )
        return inv.model_copy(
            update={"status": status, "results": results, "steps": steps, "report": report}
        )
    steps = [s.model_copy(update={"status": StepStatus.FAILED}) for s in inv.steps]
    return inv.model_copy(
        update={
            "status": InvestigationStatus.FAILED,
            "results": [],
            "steps": steps,
            "hypotheses": [],
            "recommendations": [],
            "claims": [],
            "timeline": [],
            "report": None,
        }
    )


def synthetic_history(
    replays: DemoData,
    now: datetime,
    *,
    count: int = HISTORY_COUNT,
    days: int = HISTORY_DAYS,
    seed: int = DEMO_SEED,
) -> DemoData:
    rng = random.Random(seed)  # noqa: S311 (demo data, not security)
    templates = {
        s.id: inv
        for s, inv in zip(
            sorted(replays.scenarios, key=lambda s: s.id), replays.investigations, strict=True
        )
    }
    names = [n for n in TEMPLATE_WEIGHTS if n in templates]
    weights = [TEMPLATE_WEIGHTS[n] for n in names]
    history = DemoData(scenarios=replays.scenarios)
    start = now - timedelta(days=days)
    for _ in range(count):
        template_id = rng.choices(names, weights)[0]
        template = templates[template_id]
        created = start + timedelta(seconds=rng.uniform(0, (days - 0.2) * 86400))
        duration = (
            rng.uniform(55_000, 360_000) if template_id != "S0" else rng.uniform(40_000, 120_000)
        )
        inv = _clone(template, created.replace(microsecond=0), round(duration, 1), rng)
        roll = rng.random()
        if roll < 0.05:
            inv = _degrade(inv, InvestigationStatus.FAILED, rng)
        elif roll < 0.15:
            inv = _degrade(inv, InvestigationStatus.PARTIAL, rng)
        events = replays.events[template.id]
        delta = inv.created_at - template.created_at
        history.investigations.append(inv)
        history.events[inv.id] = _shift_events(events, inv.id, delta)
    history.investigations.sort(key=lambda i: i.created_at)
    return history


async def build_demo(settings: Settings, now: datetime) -> DemoData:
    replays = await replay_demo(settings, now)
    history = synthetic_history(replays, now)
    return DemoData(
        investigations=[*history.investigations, *replays.investigations],
        events={**history.events, **replays.events},
        scenarios=replays.scenarios,
    )


async def seed_demo(store: InvestigationStore, data: DemoData) -> int:
    await store.delete_mode("demo")
    for inv in data.investigations:
        await store.save(inv)
        await store.save_events(data.events.get(inv.id, []))
    return len(data.investigations)


# --------------------------------------------------------------------------- export


def _approvals(data: DemoData) -> list[dict[str, Any]]:
    """Sample pending approvals: a Jira ticket proposal per recent root-caused incident."""
    approvals: list[dict[str, Any]] = []
    for inv in sorted(data.investigations, key=lambda i: i.created_at, reverse=True):
        report = inv.report
        if report is None or report.root_cause_hypothesis_id is None or len(approvals) >= 3:
            continue
        service = inv.context.service if inv.context else None
        approvals.append(
            {
                "id": f"act-demo{len(approvals) + 1:04d}",
                "action": "create_ticket",
                "capability": "tickets",
                "tool": "jira_create_issue",
                "arguments": {
                    "project_key": "OPS",
                    "issue_type": "Incident",
                    "summary": f"[{service}] {inv.incident.title}"[:120],
                    "description": report.summary,
                    "labels": [service] if service else [],
                },
                "reason": f"Track the root cause found by {inv.id}",
                "risk": "low",
                "status": "pending",
                "requested_by": "aiops-demo",
                "investigation_id": inv.id,
                "created_at": (inv.completed_at or inv.created_at).isoformat(),
            }
        )
    return approvals


def _services(settings: Settings) -> list[dict[str, Any]]:
    catalog = ServiceCatalog.from_settings(settings)
    return [
        {
            "name": s.name,
            "description": s.description,
            "owners": s.owners,
            "depends_on": s.depends_on,
            "environments": sorted(s.environments) or catalog.environments,
            "runbooks": s.runbooks,
        }
        for s in catalog.services
    ]


def _agents(data: DemoData) -> list[dict[str, Any]]:
    from aiops.agents.registry import AGENTS

    stats = {a["name"]: a for a in agent_stats(data.investigations)}
    return [
        {
            "name": spec.name,
            "version": spec.version,
            "description": spec.description,
            "capabilities": spec.capabilities,
            "last_run_at": stats.get(spec.name, {}).get("last_run_at"),
            "success_rate_7d": stats.get(spec.name, {}).get("success_rate"),
            "p50_ms": stats.get(spec.name, {}).get("p50_ms"),
        }
        for spec in AGENTS.specs()
    ]


EXPORT_EXCERPT_CHARS = 400


def _compact_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Static demo files keep a short excerpt of evidence data (like the store)."""
    data = evidence.get("data") or {}
    if data:
        encoded = json.dumps(data, default=str, sort_keys=True)
        if len(encoded) > EXPORT_EXCERPT_CHARS:
            data = {"excerpt": encoded[: EXPORT_EXCERPT_CHARS - 3] + "..."}
    return {**evidence, "data": data}


def compact_investigation(inv: Investigation) -> dict[str, Any]:
    payload = inv.model_dump(mode="json")
    for result in payload["results"]:
        result["evidence"] = [_compact_evidence(e) for e in result["evidence"]]
        for call in result["tool_calls"]:
            call["arguments"] = {
                k: (v[:200] + "..." if isinstance(v, str) and len(v) > 200 else v)
                for k, v in call["arguments"].items()
            }
    return payload


def compact_event(event: InvestigationEvent) -> dict[str, Any]:
    payload = event.model_dump(mode="json")
    if event.type == "evidence_added" and "evidence" in payload["data"]:
        payload["data"]["evidence"] = _compact_evidence(payload["data"]["evidence"])
    return payload


def _write(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1, sort_keys=True, default=str) + "\n")


def export_demo(settings: Settings, data: DemoData, out_dir: Path, now: datetime) -> list[Path]:
    """Contract-shaped JSON files for a static UI (NEXT_PUBLIC_DEMO=1)."""
    newest = sorted(data.investigations, key=lambda i: i.created_at, reverse=True)
    files: dict[str, Any] = {
        "investigations.json": {"items": [summary_of(i) for i in newest], "next_cursor": None},
        "dashboard.json": dashboard_summary(data.investigations, days=HISTORY_DAYS, now=now),
        "services.json": _services(settings),
        "agents.json": _agents(data),
        "approvals.json": _approvals(data),
        "scenarios.json": [
            {
                "id": s.id,
                "title": s.title,
                "service": s.service,
                "description": s.description.strip(),
                "active": False,
            }
            for s in sorted(data.scenarios, key=lambda s: s.id)
        ],
    }
    for inv in newest:
        files[f"investigations/{inv.id}.json"] = compact_investigation(inv)
        files[f"events/{inv.id}.json"] = [compact_event(e) for e in data.events.get(inv.id, [])]
    files["manifest.json"] = {
        "generated_by": "aiops demo export",
        "generated_for": now.isoformat(),
        "contract": "docs/api/contract.md v1",
        "investigations": len(newest),
        "files": sorted(files),
    }
    written = []
    for name, payload in files.items():
        path = out_dir / name
        _write(path, payload)
        written.append(path)
    return written
