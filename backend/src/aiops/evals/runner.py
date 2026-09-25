"""Eval runner: score an agent against scenario ground truth (MASTER_PLAN.md §15).

Two modes:
  * ``replay`` (default): recorded MCP fixtures + a scripted LLM that submits the
    agent's deterministic overview. Zero tokens, deterministic, runs in CI.
  * ``live``: seed each scenario into the local stack at the current time, then run
    the agent with the configured hosted LLM.

Per scenario it records the rule-based checks from ``score_agent`` plus evidence
citation validity, tool/LLM calls, tokens and latency; the report aggregates pass
rate, false-positive rate on healthy scenarios and averages. Agent-agnostic: any
registered agent with ``scenarios/<id>/agents/<agent>.yaml`` files can be evaluated.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

import aiops.agents  # noqa: F401  (registers built-in agents)
from aiops.agents.deps import build_deps
from aiops.agents.registry import AGENTS
from aiops.core.config import Settings, find_config_dir
from aiops.core.models import AgentResult, AgentStatus, ClaimKind
from aiops.evals.replay import REPLAY_NOW, echo_responder
from aiops.evals.scenario import Scenario, load_scenarios
from aiops.evals.scoring import Check, score_agent
from aiops.llm.base import LLMProvider
from aiops.llm.fake import FakeLLMProvider

Mode = Literal["replay", "live"]
MODES: tuple[Mode, ...] = ("replay", "live")

#: Signals that don't claim a problem (a healthy scenario may report them).
BENIGN_SIGNALS = frozenset(
    {
        "no_errors",
        # tickets: context only (open tickets exist on the service / nothing related found)
        "related_open_tickets",
        "no_related_tickets",
        # knowledge: no runbook matched (nothing claimed)
        "no_relevant_docs",
    }
)

#: Default minimum pass rate per mode (live: MASTER_PLAN §15 target of >= 4/5 incidents).
DEFAULT_MIN_PASS_RATE: dict[Mode, float] = {"replay": 1.0, "live": 0.8}


class EvalError(Exception):
    """The eval can't run (bad selection, missing LLM key, ...). Message is for humans."""


@dataclass(frozen=True)
class EvalPaths:
    """Where scenarios, fixtures and reports live (derived from the repo layout)."""

    repo_root: Path
    scenarios: Path
    fixtures: Path  # <fixtures>/<agent>/<scenario>/<capability>.json
    reports: Path

    @classmethod
    def discover(cls, config_dir: Path | None = None) -> EvalPaths:
        root = (config_dir or find_config_dir()).parent
        return cls(
            repo_root=root,
            scenarios=root / "scenarios",
            fixtures=root / "backend" / "tests" / "fixtures",
            reports=root / "evals" / "reports",
        )


# --------------------------------------------------------------------------- results


class ScenarioEval(BaseModel):
    scenario: str
    title: str
    healthy: bool  # no ground-truth root cause
    status: str
    signals: list[str] = Field(default_factory=list)
    checks: list[Check] = Field(default_factory=list)
    findings: int = 0
    valid_findings: int = 0  # every cited evidence id exists (and facts cite something)
    evidence: int = 0
    tool_calls: int = 0
    llm_calls: int = 0
    tokens: int = 0
    latency_ms: float = 0.0
    model: str | None = None
    prompt_version: str | None = None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def false_positive(self) -> bool:
        """A healthy scenario where the agent claims it found a problem."""
        claims_problem = self.status == AgentStatus.SUCCESS.value or any(
            s not in BENIGN_SIGNALS for s in self.signals
        )
        return self.healthy and claims_problem

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]


class EvalSummary(BaseModel):
    scenarios: int
    passed: int
    pass_rate: float
    healthy_scenarios: int
    false_positives: int
    false_positive_rate: float | None  # None when no healthy scenario was evaluated
    findings: int
    valid_findings: int
    citation_validity: float
    avg_tool_calls: float
    avg_llm_calls: float
    avg_tokens: float
    total_tokens: int
    avg_latency_ms: float
    p50_latency_ms: float

    @classmethod
    def of(cls, results: Sequence[ScenarioEval]) -> EvalSummary:
        n = len(results)
        healthy = [r for r in results if r.healthy]
        false_positives = sum(r.false_positive for r in healthy)
        findings = sum(r.findings for r in results)
        valid = sum(r.valid_findings for r in results)
        latencies = [r.latency_ms for r in results]

        def avg(values: Sequence[float]) -> float:
            return round(sum(values) / len(values), 2) if values else 0.0

        return cls(
            scenarios=n,
            passed=sum(r.passed for r in results),
            pass_rate=round(sum(r.passed for r in results) / n, 4) if n else 0.0,
            healthy_scenarios=len(healthy),
            false_positives=false_positives,
            false_positive_rate=round(false_positives / len(healthy), 4) if healthy else None,
            findings=findings,
            valid_findings=valid,
            citation_validity=round(valid / findings, 4) if findings else 1.0,
            avg_tool_calls=avg([r.tool_calls for r in results]),
            avg_llm_calls=avg([r.llm_calls for r in results]),
            avg_tokens=avg([r.tokens for r in results]),
            total_tokens=sum(r.tokens for r in results),
            avg_latency_ms=avg(latencies),
            p50_latency_ms=round(statistics.median(latencies), 2) if latencies else 0.0,
        )


class EvalReport(BaseModel):
    agent: str
    mode: Mode
    environment: str
    generated_at: datetime
    anchor: str  # replay: fixture time; live: "now at seeding"
    results: list[ScenarioEval]
    summary: EvalSummary

    @property
    def model(self) -> str | None:
        return next((r.model for r in self.results if r.model), None)

    @property
    def prompt_version(self) -> str | None:
        return next((r.prompt_version for r in self.results if r.prompt_version), None)

    def basename(self) -> str:
        return f"{self.generated_at:%Y-%m-%d}-{self.agent}-{self.mode}"


# --------------------------------------------------------------------------- scoring


def citation_counts(result: AgentResult) -> tuple[int, int]:
    """(findings, findings whose evidence ids all exist; facts/observations must cite)."""
    known = {e.id for e in result.evidence}
    valid = 0
    for finding in result.findings:
        cites_known = all(eid in known for eid in finding.evidence_ids)
        needs_evidence = finding.kind in (ClaimKind.FACT, ClaimKind.OBSERVATION)
        if cites_known and (finding.evidence_ids or not needs_evidence):
            valid += 1
    return len(result.findings), valid


def evaluate_result(scenario: Scenario, agent: str, result: AgentResult) -> ScenarioEval:
    card = score_agent(scenario.id, result, scenario.agents[agent])
    findings, valid = citation_counts(result)
    checks = [
        *card.checks,
        Check(
            name="evidence_citations",
            passed=valid == findings,
            detail=f"{valid}/{findings} findings cite existing evidence",
        ),
    ]
    return ScenarioEval(
        scenario=scenario.id,
        title=scenario.title,
        healthy=scenario.healthy,
        status=result.status.value,
        signals=list(result.signals),
        checks=checks,
        findings=findings,
        valid_findings=valid,
        evidence=len(result.evidence),
        tool_calls=len(result.tool_calls),
        llm_calls=result.usage.calls,
        tokens=result.usage.total_tokens,
        latency_ms=round(result.duration_ms, 2),
        model=result.model,
        prompt_version=result.prompt_version,
        error=result.error,
    )


def errored(scenario: Scenario, message: str) -> ScenarioEval:
    return ScenarioEval(
        scenario=scenario.id,
        title=scenario.title,
        healthy=scenario.healthy,
        status="error",
        checks=[Check(name="runnable", passed=False, detail=message)],
        error=message,
    )


# --------------------------------------------------------------------------- running


def select_scenarios(
    scenarios: Sequence[Scenario], agent: str, ids: Sequence[str]
) -> list[Scenario]:
    """Requested scenarios (default: all with ``agents/<agent>.yaml``), in id order."""
    eligible = [s for s in scenarios if agent in s.agents]
    if not ids:
        if not eligible:
            raise EvalError(f"No scenario defines expectations for agent '{agent}'.")
        return eligible
    by_id = {s.id.upper(): s for s in scenarios}
    selected: list[Scenario] = []
    for raw in ids:
        scenario = by_id.get(raw.upper())
        if scenario is None:
            raise EvalError(f"Unknown scenario '{raw}' (known: {', '.join(sorted(by_id))}).")
        if agent not in scenario.agents:
            raise EvalError(f"Scenario {scenario.id} has no agents/{agent}.yaml expectations.")
        if scenario not in selected:
            selected.append(scenario)
    return sorted(selected, key=lambda s: s.id)


def require_live_llm(settings: Settings) -> None:
    """Live evals need a hosted LLM; fail with instructions instead of a stack trace."""
    llm = settings.llm
    key = llm.api_key.get_secret_value() if llm.api_key else ""
    if llm.provider != "openai_compat" or not key or not llm.models.get("agent"):
        raise EvalError(
            "Live evals use the configured hosted LLM, but none is configured. Set "
            "LLM_PROVIDER=openai_compat, OPENAI_COMPAT_API_KEY and LLM_MODEL_AGENT in .env "
            "(free tiers: docs/setup/zero-cost.md), or use --mode replay (zero tokens)."
        )


def seed_live(scenario: Scenario, capabilities: Sequence[str], es_url: str) -> datetime:
    """Seed the data sources the agent reads for ``scenario``; returns the window end."""
    from aiops.seed.elasticsearch import SeedError, seed_scenario_logs
    from aiops.seed.logs import SCENARIOS as LOG_SCENARIOS

    now = datetime.now(UTC)
    if "logs" in capabilities:
        if scenario.id not in LOG_SCENARIOS:
            raise EvalError(f"No log generator for scenario {scenario.id}.")
        try:
            window, _ = seed_scenario_logs(es_url, scenario.id, now)
        except SeedError as exc:
            raise EvalError(f"Seeding {scenario.id} failed: {exc}") from exc
        return window.now
    return now


LLMFactory = Callable[[], LLMProvider]


async def run_eval(
    settings: Settings,
    agent: str,
    *,
    mode: Mode = "replay",
    scenario_ids: Sequence[str] = (),
    paths: EvalPaths | None = None,
    llm_factory: LLMFactory | None = None,
    es_url: str | None = None,
    on_result: Callable[[ScenarioEval], None] | None = None,
) -> EvalReport:
    """Run ``agent`` on each selected scenario and score it.

    ``llm_factory`` overrides the LLM (replay default: the echo responder; live default:
    the configured provider). Passing one in live mode skips the LLM-key check, which
    lets tests exercise live seeding with a fake LLM.
    """
    if mode not in MODES:
        raise EvalError(f"Unknown mode '{mode}' (use one of {', '.join(MODES)}).")
    agent_cls = AGENTS.get(agent)
    paths = paths or EvalPaths.discover(settings.config_dir)
    scenarios = select_scenarios(load_scenarios(paths.scenarios), agent, scenario_ids)
    if mode == "live" and llm_factory is None:
        require_live_llm(settings)
    es_url = es_url or os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")

    results: list[ScenarioEval] = []
    for scenario in scenarios:
        replay_dir: Path | None = None
        llm: LLMProvider | None
        if mode == "replay":
            replay_dir = paths.fixtures / agent / scenario.id
            if not replay_dir.is_dir():
                evaluation = errored(scenario, f"No recorded fixtures in {replay_dir}")
                results.append(evaluation)
                if on_result:
                    on_result(evaluation)
                continue
            now = REPLAY_NOW
            llm = llm_factory() if llm_factory else FakeLLMProvider(responder=echo_responder())
        else:
            now = await asyncio.to_thread(seed_live, scenario, agent_cls.spec.capabilities, es_url)
            llm = llm_factory() if llm_factory else None  # None = configured provider

        deps = build_deps(settings, llm=llm, replay_dir=replay_dir)
        result = await agent_cls(deps).run(scenario.task(agent, now))
        evaluation = evaluate_result(scenario, agent, result)
        results.append(evaluation)
        if on_result:
            on_result(evaluation)

    return EvalReport(
        agent=agent,
        mode=mode,
        environment=settings.environment,
        generated_at=datetime.now(UTC),
        anchor=REPLAY_NOW.isoformat() if mode == "replay" else "seeded at run time",
        results=results,
        summary=EvalSummary.of(results),
    )


# --------------------------------------------------------------------------- reports


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


MODE_NOTES: dict[Mode, str] = {
    "replay": (
        "Replay: recorded MCP fixtures (window ending {anchor}) and a scripted LLM that "
        "submits the agent's deterministic overview with status `no_signal`. Zero tokens. "
        "This scores the deterministic investigation and guardrails, not LLM reasoning."
    ),
    "live": (
        "Live: each scenario seeded into the local stack at run time, then investigated "
        "with the configured hosted LLM."
    ),
}


def render_markdown(report: EvalReport) -> str:
    s = report.summary
    fp = (
        f"{s.false_positives}/{s.healthy_scenarios} ({_pct(s.false_positive_rate)})"
        if s.healthy_scenarios
        else "n/a (no healthy scenario)"
    )
    lines = [
        f"# Eval scorecard: `{report.agent}` agent ({report.mode})",
        "",
        f"Generated {report.generated_at:%Y-%m-%d %H:%M} UTC · environment `{report.environment}` · "
        f"model `{report.model or 'n/a'}` · prompt `{report.prompt_version or 'n/a'}`",
        "",
        MODE_NOTES[report.mode].format(anchor=report.anchor),
        "",
        f"Reproduce: `make eval AGENT={report.agent} MODE={report.mode}`",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Pass rate | {s.passed}/{s.scenarios} ({_pct(s.pass_rate)}) |",
        f"| False-positive rate (healthy scenarios) | {fp} |",
        f"| Findings with valid evidence citations | {s.valid_findings}/{s.findings} ({_pct(s.citation_validity)}) |",
        f"| Avg tool calls | {s.avg_tool_calls:g} |",
        f"| Avg LLM calls | {s.avg_llm_calls:g} |",
        f"| Avg tokens (total) | {s.avg_tokens:g} ({s.total_tokens}) |",
        f"| Latency avg / p50 | {s.avg_latency_ms:.0f} ms / {s.p50_latency_ms:.0f} ms |",
        "",
        "## Scenarios",
        "",
        "| Scenario | Result | Status | Signals | Evidence | Tool calls | LLM calls | Tokens | Latency |",
        "|----------|--------|--------|---------|---------:|-----------:|----------:|-------:|--------:|",
    ]
    for r in report.results:
        result = "PASS" if r.passed else "FAIL"
        if r.false_positive:
            result += " (false positive)"
        lines.append(
            f"| {r.scenario} {_cell(r.title)} | {result} | {r.status} | "
            f"{', '.join(r.signals) or '-'} | {r.evidence} | {r.tool_calls} | {r.llm_calls} | "
            f"{r.tokens} | {r.latency_ms:.0f} ms |"
        )
    lines += ["", "## Checks", ""]
    for r in report.results:
        lines.append(f"### {r.scenario}: {'PASS' if r.passed else 'FAIL'}")
        lines.append("")
        for c in r.checks:
            detail = f": {_cell(c.detail)}" if c.detail else ""
            lines.append(f"- [{'x' if c.passed else ' '}] `{c.name}`{detail}")
        if r.error:
            lines.append(f"- error: {_cell(r.error)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def report_json(report: EvalReport) -> str:
    payload = report.model_dump(mode="json")
    for item, result in zip(payload["results"], report.results, strict=True):
        item["passed"] = result.passed
        item["false_positive"] = result.false_positive
    return json.dumps(payload, indent=2) + "\n"


def write_report(report: EvalReport, out_dir: Path) -> tuple[Path, Path]:
    """Write ``<date>-<agent>-<mode>.md`` and its JSON twin; returns both paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / f"{report.basename()}.md"
    json_path = out_dir / f"{report.basename()}.json"
    markdown.write_text(render_markdown(report))
    json_path.write_text(report_json(report))
    return markdown, json_path
