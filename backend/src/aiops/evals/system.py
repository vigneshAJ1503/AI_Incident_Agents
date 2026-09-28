"""Full-system evaluation (PR-040, MASTER_PLAN §15): ONE command, ONE scorecard.

``aiops evaluate`` runs, for one profile and mode:

* **per-agent evals** (``runner.run_eval``) for every selected agent: pass rate,
  false-positive rate, citation validity, tool calls, tokens, latency, cost;
* **planner cases**: service identification from the question alone, for every scenario
  question plus the negative/ambiguous cases in ``scenarios/planner-cases.yaml``
  (clarification instead of a guess, unknown services never invented);
* **end-to-end investigations** (``investigation.evaluate_investigations``): rule-based
  root-cause accuracy, optional LLM-judge accuracy, confidence calibration (Brier score),
  false positives, claim citations, time to report, tokens and cost;

and writes ``evals/reports/<date>-system-<mode>.md`` + ``.json`` with a reproducibility
header (profile, provider, models, prompt versions incl. provider fragments, agent
versions, git SHA). ``compare`` is the regression gate against a committed baseline
(``evals/baselines/<mode>.json``).
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aiops.agents.registry import AGENTS
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import (
    ConfigError,
    ModelPrice,
    Settings,
    format_validation_error,
    load_yaml,
)
from aiops.core.models import TokenUsage
from aiops.evals.investigation import InvestigationEval, evaluate_investigations
from aiops.evals.judge import judge_prompt, judge_root_cause, judge_unavailable
from aiops.evals.runner import (
    EvalError,
    EvalPaths,
    EvalReport,
    LLMFactory,
    Mode,
    ScenarioEval,
    run_eval,
)
from aiops.evals.scenario import Scenario, load_scenarios
from aiops.evals.scoring import Check
from aiops.llm.base import LLMProvider
from aiops.orchestrator.planner import Planner, PlanRequest

PLANNER_CASES_FILE = "planner-cases.yaml"
BASELINES_DIR = "baselines"


def _avg(values: Sequence[float]) -> float:
    return round(sum(values) / len(values), 4) if values else 0.0


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


# --------------------------------------------------------------------------- cost


def provider_host(settings: Settings) -> str:
    """The LLM provider as recorded in reports: ``fake`` or the API host (api.groq.com)."""
    llm = settings.llm
    if llm.provider == "fake":
        return "fake"
    return urlparse(llm.base_url or "").hostname or llm.provider


def price_for(settings: Settings, model: str | None) -> ModelPrice:
    """``evals.pricing``: model id, then provider host, then ``*``; default 0 (free tiers)."""
    table = settings.evals.pricing
    for key in (model, provider_host(settings), "*"):
        if key and key in table:
            return table[key]
    return ModelPrice()


def cost_usd(price: ModelPrice, usage: TokenUsage) -> float:
    return round(
        (usage.input_tokens * price.input + usage.output_tokens * price.output) / 1_000_000, 6
    )


# --------------------------------------------------------------------------- planner cases


class PlannerCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    title: str
    question: str
    expect: Literal["service", "clarification"]
    service: str | None = None
    candidates: list[str] = Field(default_factory=list)  # must all be offered
    never: list[str] = Field(default_factory=list)  # must never be the planned service


class PlannerCasesFile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cases: list[PlannerCase] = Field(default_factory=list)


def load_planner_cases(path: Path) -> list[PlannerCase]:
    if not path.is_file():
        return []
    try:
        return PlannerCasesFile.model_validate(load_yaml(path)).cases
    except ValidationError as err:
        raise ConfigError(format_validation_error(err, path)) from err


def scenario_cases(scenarios: Sequence[Scenario]) -> list[PlannerCase]:
    """Every scenario question must lead to the scenario's service."""
    return [
        PlannerCase(
            id=s.id,
            title=s.title,
            question=s.question,
            expect="service",
            service=s.service,
        )
        for s in scenarios
        if s.service
    ]


class PlannerEval(BaseModel):
    case: str
    title: str
    question: str
    expect: str
    expected_service: str | None = None
    service: str | None = None
    clarification: bool = False
    candidates: list[str] = Field(default_factory=list)
    invented: bool = False  # planned a service outside the catalog or listed in `never`
    checks: list[Check] = Field(default_factory=list)
    tokens: int = 0

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


def score_plan(
    case: PlannerCase,
    service: str | None,
    clarification: bool,
    candidates: Sequence[str],
    known: set[str],
) -> tuple[list[Check], bool]:
    invented = service is not None and (service not in known or service in case.never)
    checks = [Check(name="no_invented_service", passed=not invented, detail=service or "-")]
    if case.expect == "service":
        checks.append(
            Check(
                name=f"service:{case.service}",
                passed=service == case.service and not clarification,
                detail="asked for clarification" if clarification else f"got {service}",
            )
        )
    else:
        checks.append(
            Check(
                name="asks_clarification",
                passed=clarification and service is None,
                detail=f"planned {service}" if service else "",
            )
        )
        missing = [c for c in case.candidates if c not in candidates]
        if case.candidates:
            checks.append(
                Check(
                    name="offers_candidates",
                    passed=not missing,
                    detail=f"missing {missing}" if missing else ", ".join(candidates),
                )
            )
    return checks, invented


async def evaluate_planner(
    settings: Settings, cases: Sequence[PlannerCase], llm: LLMProvider | None = None
) -> list[PlannerEval]:
    """Plan each question (no agent runs). ``llm`` = the planner's LLM fallback (live)."""
    catalog = ServiceCatalog.from_settings(settings)
    known = {s.name for s in catalog.services}
    planner = Planner(settings, catalog, AGENTS, llm=llm)
    results: list[PlannerEval] = []
    for case in cases:
        plan = await planner.plan(PlanRequest(question=case.question))
        service = plan.context.service
        checks, invented = score_plan(
            case, service, plan.needs_clarification, plan.candidates, known
        )
        results.append(
            PlannerEval(
                case=case.id,
                title=case.title,
                question=case.question,
                expect=case.expect,
                expected_service=case.service,
                service=service,
                clarification=plan.needs_clarification,
                candidates=list(plan.candidates),
                invented=invented,
                checks=checks,
                tokens=plan.usage.total_tokens,
            )
        )
    return results


# --------------------------------------------------------------------------- summaries


class AgentScore(BaseModel):
    """One agent's per-scenario evals, summarized (the scorecard's per-agent row)."""

    agent: str
    version: str
    prompt: str | None = None
    model: str | None = None
    scenarios: int
    passed: int
    pass_rate: float
    false_positive_rate: float | None
    citation_validity: float
    avg_tool_calls: float
    avg_tokens: float
    total_tokens: int
    p50_latency_ms: float
    cost_usd: float
    failed: dict[str, list[str]] = Field(default_factory=dict)  # scenario -> failed checks

    @classmethod
    def of(cls, report: EvalReport, settings: Settings) -> AgentScore:
        s = report.summary
        usage = TokenUsage(
            input_tokens=sum(r.input_tokens for r in report.results),
            output_tokens=sum(r.output_tokens for r in report.results),
        )
        return cls(
            agent=report.agent,
            version=AGENTS.get(report.agent).spec.version,
            prompt=report.prompt_version,
            model=report.model,
            scenarios=s.scenarios,
            passed=s.passed,
            pass_rate=s.pass_rate,
            false_positive_rate=s.false_positive_rate,
            citation_validity=s.citation_validity,
            avg_tool_calls=s.avg_tool_calls,
            avg_tokens=s.avg_tokens,
            total_tokens=s.total_tokens,
            p50_latency_ms=s.p50_latency_ms,
            cost_usd=cost_usd(price_for(settings, report.model), usage),
            failed={
                r.scenario: [c.name for c in r.failed_checks]
                for r in report.results
                if not r.passed
            },
        )


class PlannerSummary(BaseModel):
    cases: int
    passed: int
    pass_rate: float
    #: Questions that must resolve to a service: resolved to the right one.
    service_accuracy: float | None
    #: Questions that must be clarified: the planner asked instead of guessing.
    clarification_accuracy: float | None
    invented_services: int

    @classmethod
    def of(cls, results: Sequence[PlannerEval]) -> PlannerSummary:
        service = [r for r in results if r.expect == "service"]
        clarify = [r for r in results if r.expect == "clarification"]
        return cls(
            cases=len(results),
            passed=sum(r.passed for r in results),
            pass_rate=_rate(sum(r.passed for r in results), len(results)) or 0.0,
            service_accuracy=_rate(
                sum(r.service == r.expected_service and not r.clarification for r in service),
                len(service),
            ),
            clarification_accuracy=_rate(
                sum(r.clarification and r.service is None for r in clarify), len(clarify)
            ),
            invented_services=sum(r.invented for r in results),
        )


class InvestigationSummary(BaseModel):
    scenarios: int
    passed: int
    pass_rate: float
    #: Incident scenarios (S1-S5) whose root cause matches the ground truth (rule-based).
    incidents: int
    root_cause_correct: int
    root_cause_accuracy: float | None
    #: Same, by the LLM judge (None = not judged).
    judged: int
    judge_accuracy: float | None
    healthy_scenarios: int
    false_positives: int
    false_positive_rate: float | None
    #: Mean (confidence - correct)^2 over all scenarios: 0 = perfectly calibrated.
    brier_score: float
    claims: int
    valid_claims: int
    citation_validity: float
    avg_time_to_report_ms: float
    p50_time_to_report_ms: float
    total_tokens: int
    avg_tokens: float
    total_cost_usd: float
    avg_cost_usd: float

    @classmethod
    def of(cls, results: Sequence[InvestigationEval]) -> InvestigationSummary:
        n = len(results)
        incidents = [r for r in results if not r.healthy]
        healthy = [r for r in results if r.healthy]
        judged = [r for r in incidents if r.judge is not None]
        durations = [r.duration_ms for r in results]
        claims = sum(r.claims for r in results)
        valid = sum(r.valid_claims for r in results)
        return cls(
            scenarios=n,
            passed=sum(r.passed for r in results),
            pass_rate=_rate(sum(r.passed for r in results), n) or 0.0,
            incidents=len(incidents),
            root_cause_correct=sum(r.root_cause_correct for r in incidents),
            root_cause_accuracy=_rate(sum(r.root_cause_correct for r in incidents), len(incidents)),
            judged=len(judged),
            judge_accuracy=_rate(
                sum(bool(r.judge and r.judge.correct) for r in judged), len(judged)
            ),
            healthy_scenarios=len(healthy),
            false_positives=sum(r.false_positive for r in healthy),
            false_positive_rate=_rate(sum(r.false_positive for r in healthy), len(healthy)),
            brier_score=_avg([r.brier for r in results]),
            claims=claims,
            valid_claims=valid,
            citation_validity=_rate(valid, claims) or 1.0,
            avg_time_to_report_ms=round(_avg(durations), 1),
            p50_time_to_report_ms=round(statistics.median(durations), 1) if durations else 0.0,
            total_tokens=sum(r.tokens for r in results),
            avg_tokens=round(_avg([r.tokens for r in results]), 1),
            total_cost_usd=round(sum(r.cost_usd for r in results), 6),
            avg_cost_usd=round(_avg([r.cost_usd for r in results]), 6),
        )


# --------------------------------------------------------------------------- reproducibility


class RunInfo(BaseModel):
    """What produced the numbers: enough to reproduce or explain a change."""

    mode: Mode
    profile: str
    provider: str  # the configured provider (API host)
    #: What actually answered: the configured provider, or the scripted LLM in replay.
    llm_used: str = ""
    models: dict[str, str] = Field(default_factory=dict)  # configured role -> model
    models_used: list[str] = Field(default_factory=list)
    #: Prompt refs by agent (``logs/v3@sha+providers/logs/elasticsearch/v1@sha``).
    prompts: dict[str, str] = Field(default_factory=dict)
    agent_versions: dict[str, str] = Field(default_factory=dict)
    git_sha: str = "unknown"
    git_dirty: bool = False
    generated_at: datetime
    judge: str = "off"


def git_state(repo_root: Path) -> tuple[str, bool]:
    """(sha, dirty) of the repo; ``$GITHUB_SHA`` in CI when git is unavailable."""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
        return sha, bool(status)
    except (OSError, subprocess.SubprocessError):
        return os.environ.get("GITHUB_SHA", "unknown"), False


def collect_prompts(
    agent_scores: Sequence[AgentScore], investigations: Sequence[InvestigationEval]
) -> dict[str, str]:
    prompts: dict[str, str] = {}
    for inv in investigations:
        for ref in filter(None, inv.versions.get("prompts", "").split(",")):
            prompts.setdefault(ref.split("/", 1)[0], ref)
    for score in agent_scores:
        if score.prompt:
            prompts[score.agent] = score.prompt
    return dict(sorted(prompts.items()))


# --------------------------------------------------------------------------- the report


class SystemReport(BaseModel):
    info: RunInfo
    agents: list[AgentScore] = Field(default_factory=list)
    agent_results: dict[str, list[ScenarioEval]] = Field(default_factory=dict)
    planner: list[PlannerEval] = Field(default_factory=list)
    planner_summary: PlannerSummary | None = None
    investigations: list[InvestigationEval] = Field(default_factory=list)
    investigation_summary: InvestigationSummary | None = None
    notes: list[str] = Field(default_factory=list)

    def basename(self) -> str:
        return f"{self.info.generated_at:%Y-%m-%d}-system-{self.info.mode}"


AgentSelection = Literal["all"] | Sequence[str]


def select_agents(selection: AgentSelection, scenarios: Sequence[Scenario]) -> list[str]:
    """``all`` = every registered agent with scenario expectations."""
    with_expectations = {name for s in scenarios for name in s.agents}
    if selection == "all":
        return [spec.name for spec in AGENTS.specs() if spec.name in with_expectations]
    names: list[str] = []
    for name in selection:
        if name not in AGENTS.names():
            raise EvalError(f"Unknown agent '{name}' (known: {', '.join(AGENTS.names())}).")
        if name not in with_expectations:
            raise EvalError(f"No scenario defines expectations for agent '{name}'.")
        if name not in names:
            names.append(name)
    return names


async def run_system_eval(
    settings: Settings,
    *,
    agents: AgentSelection = "all",
    investigations: bool = True,
    mode: Mode = "replay",
    scenario_ids: Sequence[str] = (),
    judge: bool | None = None,
    paths: EvalPaths | None = None,
    llm_factory: LLMFactory | None = None,
    judge_factory: LLMFactory | None = None,
    progress: Callable[[str], None] | None = None,
) -> SystemReport:
    """Everything selected, in one report. ``llm_factory`` (tests) replaces the live LLM;
    ``judge_factory`` replaces the judge's LLM (and skips its key check)."""
    paths = paths or EvalPaths.discover(settings.config_dir)
    say = progress or (lambda _msg: None)
    scenarios = load_scenarios(paths.scenarios)
    wanted = {s.upper() for s in scenario_ids}
    known = {s.id.upper() for s in scenarios}
    unknown = sorted(wanted - known)
    if unknown:
        raise EvalError(f"Unknown scenario(s) {unknown} (known: {', '.join(sorted(known))}).")
    selected = [s for s in scenarios if not wanted or s.id.upper() in wanted]
    notes: list[str] = []

    # --- per agent
    agent_scores: list[AgentScore] = []
    agent_results: dict[str, list[ScenarioEval]] = {}
    for name in select_agents(agents, scenarios) if agents else []:
        ids = [s.id for s in selected if name in s.agents]
        if not ids:
            continue
        say(f"agent {name}: {len(ids)} scenario(s)")
        report = await run_eval(
            settings, name, mode=mode, scenario_ids=ids, paths=paths, llm_factory=llm_factory
        )
        agent_scores.append(AgentScore.of(report, settings))
        agent_results[name] = report.results

    # --- planner + investigations
    planner: list[PlannerEval] = []
    evals: list[InvestigationEval] = []
    judge_note = "off"
    if investigations:
        cases = scenario_cases(selected)
        if not wanted:
            cases += load_planner_cases(paths.scenarios / PLANNER_CASES_FILE)
        planner_llm = None
        if mode == "live" and settings.orchestrator.llm_planner_fallback:
            planner_llm = llm_factory() if llm_factory else _configured_llm(settings)
        say(f"planner: {len(cases)} question(s)")
        planner = await evaluate_planner(settings, cases, planner_llm)

        say(f"investigations: {len(selected)} scenario(s) end to end")
        evals = await evaluate_investigations(
            settings,
            [s.id for s in selected],
            mode=mode,
            llm_factory=llm_factory,
            paths=paths,
        )
        by_id = {s.id: s for s in selected}
        for inv in evals:
            model = inv.models[0] if inv.models else None
            inv.cost_usd = cost_usd(
                price_for(settings, model),
                TokenUsage(input_tokens=inv.input_tokens, output_tokens=inv.output_tokens),
            )
        judge_note = await _judge(settings, evals, by_id, judge, judge_factory, notes)

    info = RunInfo(
        mode=mode,
        profile=settings.profile,
        provider=provider_host(settings),
        llm_used=(
            "scripted replay LLM (zero tokens)"
            if mode == "replay"
            else provider_host(settings) + (" (tests: injected LLM)" if llm_factory else "")
        ),
        models={role: model for role, model in settings.llm.models.items() if model},
        models_used=sorted(
            {m for s in agent_scores if s.model for m in s.model.split(",")}
            | {m for inv in evals for m in inv.models}
        ),
        prompts=collect_prompts(agent_scores, evals),
        agent_versions={
            **{spec.name: spec.version for spec in AGENTS.specs()},
            **({"rca": evals[0].versions["rca"]} if evals and "rca" in evals[0].versions else {}),
            **(
                {"orchestrator": evals[0].versions["orchestrator"]}
                if evals and "orchestrator" in evals[0].versions
                else {}
            ),
        },
        generated_at=datetime.now(UTC),
        judge=judge_note,
    )
    info.git_sha, info.git_dirty = git_state(paths.repo_root)
    return SystemReport(
        info=info,
        agents=agent_scores,
        agent_results=agent_results,
        planner=planner,
        planner_summary=PlannerSummary.of(planner) if planner else None,
        investigations=evals,
        investigation_summary=InvestigationSummary.of(evals) if evals else None,
        notes=notes,
    )


def _configured_llm(settings: Settings) -> LLMProvider | None:
    from aiops.llm.factory import create_provider

    try:
        return create_provider(settings.llm)
    except ConfigError:
        return None


async def _judge(
    settings: Settings,
    evals: Sequence[InvestigationEval],
    scenarios: dict[str, Scenario],
    wanted: bool | None,
    factory: LLMFactory | None,
    notes: list[str],
) -> str:
    """Run the optional LLM judge on incident scenarios; returns the header note."""
    enabled = settings.evals.llm_judge if wanted is None else wanted
    if not enabled:
        return "off (rule-based root-cause scoring only; enable with --judge)"
    reason = None if factory else judge_unavailable(settings)
    if reason:
        notes.append(reason)
        return "skipped: no LLM key configured"
    llm = factory() if factory else _configured_llm(settings)
    if llm is None:
        notes.append("LLM judge skipped: the configured LLM could not be created.")
        return "skipped"
    prompt = judge_prompt(settings)
    for inv in evals:
        scenario = scenarios[inv.scenario]
        if scenario.healthy:
            continue
        verdict, usage, error = await judge_root_cause(
            llm, prompt, scenario.root_cause, inv.root_cause
        )
        inv.judge, inv.judge_error = verdict, error
        inv.tokens += usage.total_tokens
        inv.input_tokens += usage.input_tokens
        inv.output_tokens += usage.output_tokens
    return f"on ({prompt.ref}, model {getattr(llm, 'name', '?')})"


# --------------------------------------------------------------------------- baseline gate


class Baseline(BaseModel):
    """The committed reference numbers the regression gate compares against."""

    mode: Mode
    #: agent -> {pass_rate, false_positive_rate}
    agents: dict[str, dict[str, float | None]] = Field(default_factory=dict)
    #: pass_rate, root_cause_accuracy, false_positive_rate
    investigations: dict[str, float | None] = Field(default_factory=dict)
    #: pass_rate, service_accuracy, clarification_accuracy
    planner: dict[str, float | None] = Field(default_factory=dict)
    source: str = ""  # the report it was taken from


#: Metrics where lower is worse (a drop fails) / higher is worse (a rise fails).
HIGHER_IS_BETTER = (
    "pass_rate",
    "root_cause_accuracy",
    "service_accuracy",
    "clarification_accuracy",
)
LOWER_IS_BETTER = ("false_positive_rate",)


def baseline_of(report: SystemReport) -> Baseline:
    inv = report.investigation_summary
    planner = report.planner_summary
    return Baseline(
        mode=report.info.mode,
        agents={
            a.agent: {"pass_rate": a.pass_rate, "false_positive_rate": a.false_positive_rate}
            for a in report.agents
        },
        investigations=(
            {
                "pass_rate": inv.pass_rate,
                "root_cause_accuracy": inv.root_cause_accuracy,
                "false_positive_rate": inv.false_positive_rate,
            }
            if inv
            else {}
        ),
        planner=(
            {
                "pass_rate": planner.pass_rate,
                "service_accuracy": planner.service_accuracy,
                "clarification_accuracy": planner.clarification_accuracy,
            }
            if planner
            else {}
        ),
        source=report.basename(),
    )


class Regression(BaseModel):
    scope: str  # "agent logs", "investigations", "planner"
    metric: str
    baseline: float
    current: float

    def __str__(self) -> str:
        return f"{self.scope}: {self.metric} {self.baseline:.0%} -> {self.current:.0%}"


def _compare(
    scope: str,
    base: dict[str, float | None],
    current: dict[str, float | None],
    tolerance: float,
) -> list[Regression]:
    found: list[Regression] = []
    for metric, before in base.items():
        now = current.get(metric)
        if before is None or now is None:
            continue
        worse = (
            now < before - tolerance
            if metric in HIGHER_IS_BETTER
            else now > before + tolerance
            if metric in LOWER_IS_BETTER
            else False
        )
        if worse:
            found.append(Regression(scope=scope, metric=metric, baseline=before, current=now))
    return found


def compare(report: SystemReport, baseline: Baseline, tolerance: float = 0.0) -> list[Regression]:
    """Regressions of ``report`` against ``baseline``. Only what both measured is compared
    (``--agents logs`` doesn't fail on the other agents), and only on the full scenario
    set: a subset's rates aren't comparable."""
    current = baseline_of(report)
    regressions: list[Regression] = []
    for agent, base in baseline.agents.items():
        if agent in current.agents:
            regressions += _compare(f"agent {agent}", base, current.agents[agent], tolerance)
    regressions += _compare(
        "investigations", baseline.investigations, current.investigations, tolerance
    )
    regressions += _compare("planner", baseline.planner, current.planner, tolerance)
    return regressions


def load_baseline(path: Path) -> Baseline | None:
    if not path.is_file():
        return None
    try:
        return Baseline.model_validate_json(path.read_text())
    except ValidationError as err:
        raise ConfigError(format_validation_error(err, path)) from err


def write_baseline(report: SystemReport, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline_of(report).model_dump_json(indent=2) + "\n")
    return path


# --------------------------------------------------------------------------- rendering


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def _cell(text: str | None) -> str:
    return (text or "-").replace("|", "\\|").replace("\n", " ")


def _ms(value: float) -> str:
    return f"{value / 1000:.1f} s" if value >= 1000 else f"{value:.0f} ms"


MODE_NOTES: dict[Mode, str] = {
    "replay": (
        "Replay: recorded MCP fixtures and a scripted LLM (zero tokens, deterministic). "
        "This scores the deterministic pipeline (planning, agents, rule-based RCA, guardrails), "
        "not LLM reasoning; times are replay times, not live latency."
    ),
    "live": (
        "Live: scenarios seeded into the local stack at run time and investigated with the "
        "configured hosted LLM. Numbers vary between runs; compare several."
    ),
}


def render_markdown(report: SystemReport, regressions: Sequence[Regression] | None = None) -> str:
    info = report.info
    lines = [
        f"# System scorecard ({info.mode})",
        "",
        MODE_NOTES[info.mode],
        "",
        "## Run",
        "",
        "| | |",
        "|---|---|",
        f"| Generated | {info.generated_at:%Y-%m-%d %H:%M} UTC |",
        f"| Profile | `{info.profile}` |",
        f"| LLM provider (configured) | `{info.provider}` |",
        f"| LLM used | {_cell(info.llm_used)} |",
        f"| Models (configured) | {_cell(', '.join(f'{k}={v}' for k, v in info.models.items()) or 'none')} |",
        f"| Models (used) | {_cell(', '.join(info.models_used) or 'none')} |",
        f"| Git SHA | `{info.git_sha[:12]}`{' (uncommitted changes)' if info.git_dirty else ''} |",
        f"| LLM judge | {_cell(info.judge)} |",
        f"| Reproduce | `aiops evaluate --mode {info.mode} --profile {info.profile}` |",
        "",
    ]
    if regressions is not None:
        lines += ["## Regression gate", ""]
        lines += (
            [f"- FAIL {_cell(str(r))}" for r in regressions]
            if regressions
            else ["- PASS: no metric is worse than the baseline."]
        )
        lines.append("")
    inv = report.investigation_summary
    if inv:
        fp = f"{inv.false_positives}/{inv.healthy_scenarios} ({_pct(inv.false_positive_rate)})"
        judge = (
            f"{sum(bool(r.judge and r.judge.correct) for r in report.investigations)}/{inv.judged} "
            f"({_pct(inv.judge_accuracy)})"
            if inv.judged
            else "not judged"
        )
        lines += [
            "## Investigations (end to end)",
            "",
            "| Metric | Value | v1.0 target |",
            "|--------|-------|-------------|",
            f"| Root-cause accuracy, rule-based (incident scenarios) | {inv.root_cause_correct}/{inv.incidents} ({_pct(inv.root_cause_accuracy)}) | >= 4/5 |",
            f"| Root-cause accuracy, LLM judge | {judge} | |",
            f"| False positives (healthy scenarios) | {fp} | 0 |",
            f"| All checks pass | {inv.passed}/{inv.scenarios} ({_pct(inv.pass_rate)}) | |",
            f"| Confidence calibration (Brier score, 0 = best) | {inv.brier_score:.3f} | |",
            f"| Claims with valid evidence citations | {inv.valid_claims}/{inv.claims} ({_pct(inv.citation_validity)}) | >= 95% |",
            f"| Time to report avg / p50 | {_ms(inv.avg_time_to_report_ms)} / {_ms(inv.p50_time_to_report_ms)} | p50 < 90 s |",
            f"| Tokens total (avg) | {inv.total_tokens} ({inv.avg_tokens:g}) | |",
            f"| Cost estimate total (avg) | ${inv.total_cost_usd:.4f} (${inv.avg_cost_usd:.4f}) | tracked |",
            "",
            "| Scenario | Result | Service | Root cause (rule) | Judge | Confidence | Severity | Time | Tokens |",
            "|----------|--------|---------|-------------------|-------|-----------:|----------|-----:|-------:|",
        ]
        for r in report.investigations:
            rc = (
                "none (healthy)"
                if r.healthy and r.root_cause_correct
                else ("correct" if r.root_cause_correct else "WRONG")
            )
            verdict = (
                f"{'correct' if r.judge.correct else 'wrong'} ({r.judge.score:.2f})"
                if r.judge
                else ("error" if r.judge_error else "-")
            )
            lines.append(
                f"| {r.scenario} {_cell(r.title)} | {'PASS' if r.passed else 'FAIL'}"
                f"{' (false positive)' if r.false_positive else ''} | {r.service or '-'} | {rc} | "
                f"{verdict} | {r.confidence:.2f} | {r.severity} | {_ms(r.duration_ms)} | {r.tokens} |"
            )
        lines.append("")
        for r in report.investigations:
            failed_checks = [c for c in r.checks if not c.passed]
            if failed_checks or r.root_cause:
                lines.append(
                    f"- **{r.scenario}** root cause: {_cell(r.root_cause) if r.root_cause else 'none'}"
                )
                for c in failed_checks:
                    lines.append(f"  - [ ] `{c.name}` {_cell(c.detail) if c.detail else ''}")
                if r.judge:
                    lines.append(f"  - judge: {_cell(r.judge.reason)}")
        lines.append("")
    planner = report.planner_summary
    if planner:
        lines += [
            "## Planner (service identification from the question alone)",
            "",
            f"Service accuracy {_pct(planner.service_accuracy)} · clarification when expected "
            f"{_pct(planner.clarification_accuracy)} · invented services **{planner.invented_services}** · "
            f"all checks {planner.passed}/{planner.cases} ({_pct(planner.pass_rate)})",
            "",
            "| Case | Question | Expected | Got | Result |",
            "|------|----------|----------|-----|--------|",
        ]
        for p in report.planner:
            expected = p.expected_service if p.expect == "service" else "clarification"
            got = "clarification" if p.clarification else (p.service or "-")
            if p.clarification and p.candidates:
                got += f" ({', '.join(p.candidates)})"
            failed_names = ", ".join(c.name for c in p.checks if not c.passed)
            lines.append(
                f"| {p.case} | {_cell(p.question)} | {expected} | {_cell(got)} | "
                f"{'PASS' if p.passed else 'FAIL: ' + failed_names} |"
            )
        lines.append("")
    if report.agents:
        lines += [
            "## Agents (each on its own, per scenario)",
            "",
            "| Agent | Version | Pass rate | False positives | Valid citations | Avg tool calls | Avg tokens | p50 latency | Cost | Failed |",
            "|-------|---------|-----------|-----------------|-----------------|---------------:|-----------:|------------:|-----:|--------|",
        ]
        for a in report.agents:
            failed_agent = "; ".join(f"{s}: {', '.join(c)}" for s, c in a.failed.items()) or "-"
            lines.append(
                f"| {a.agent} | {a.version} | {a.passed}/{a.scenarios} ({_pct(a.pass_rate)}) | "
                f"{_pct(a.false_positive_rate)} | {_pct(a.citation_validity)} | {a.avg_tool_calls:g} | "
                f"{a.avg_tokens:g} | {_ms(a.p50_latency_ms)} | ${a.cost_usd:.4f} | "
                f"{_cell(failed_agent)} |"
            )
        lines.append("")
    lines += ["## Versions", "", "| Component | Version |", "|---|---|"]
    lines += [f"| agent `{k}` | {v} |" for k, v in sorted(report.info.agent_versions.items())]
    lines += [f"| prompt `{k}` | `{v}` |" for k, v in report.info.prompts.items()]
    if report.notes:
        lines += ["", "## Notes", ""]
        lines += [f"- {_cell(n)}" for n in report.notes]
    return "\n".join(lines).rstrip() + "\n"


def report_json(report: SystemReport, regressions: Sequence[Regression] | None = None) -> str:
    payload = report.model_dump(mode="json")
    for item, result in zip(payload["investigations"], report.investigations, strict=True):
        item.update(
            passed=result.passed,
            root_cause_correct=result.root_cause_correct,
            false_positive=result.false_positive,
            brier=result.brier,
        )
    for item, plan in zip(payload["planner"], report.planner, strict=True):
        item["passed"] = plan.passed
    if regressions is not None:
        payload["regressions"] = [r.model_dump() for r in regressions]
    return json.dumps(payload, indent=2) + "\n"


def write_report(
    report: SystemReport, out_dir: Path, regressions: Sequence[Regression] | None = None
) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = out_dir / f"{report.basename()}.md"
    json_path = out_dir / f"{report.basename()}.json"
    markdown.write_text(render_markdown(report, regressions))
    json_path.write_text(report_json(report, regressions))
    return markdown, json_path
