"""Planner (PR-030, UC-10 step 1, UC-13): question -> incident context -> plan (a DAG).

Deterministic first, the LLM (``fast`` role) only as a fallback:

1. **Service**: catalog names and aliases found in the question (word n-grams). Never
   invented: an LLM suggestion must resolve through the catalog too. Several services
   -> the caller when the others are its dependencies, otherwise ``needs_clarification``
   with the candidates. None -> the LLM fallback, else ``needs_clarification``.
2. **Environment**: catalog environment names/aliases ("prod" -> production); default:
   the catalog's first environment.
3. **Time range**: "last 30m", "past 2 hours", "since 10:15", "since
   2026-09-25T10:00Z", "between 10:00 and 10:30", "20 minutes ago"; default
   ``orchestrator.default_window``.
4. **Symptoms**: a small vendor-neutral vocabulary (http_5xx, latency, timeouts, ...).

The plan comes from the agent registry + the profile: every registered agent whose
capabilities are enabled (and that isn't disabled in ``agents.<name>.enabled``). Agents
listed in ``orchestrator.round2_agents`` (default knowledge, tickets) need round-1
findings as input and run in round 2; everything else (code and k8s included) runs in
round 1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

from aiops.agents.base import AgentSpec
from aiops.agents.registry import AgentRegistry
from aiops.core.catalog import ServiceCatalog, ServiceEntry, normalize
from aiops.core.config import Settings
from aiops.core.models import (
    IncidentContext,
    InvestigationStep,
    TimeRange,
    TokenUsage,
    parse_duration,
    parse_timestamp,
    utcnow,
)
from aiops.llm.base import ChatMessage, LLMError, LLMProvider
from aiops.llm.structured import generate_structured

MAX_NGRAM = 3
_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_UNIT = r"(s|sec|secs|seconds?|m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)"
_LAST = re.compile(
    rf"\b(?:last|past|previous|over the last|in the last|for the last)\s+(?:(\d+)\s*{_UNIT}|({_UNIT[1:-1]}))\b",
    re.IGNORECASE,
)
_AGO = re.compile(rf"\b(\d+)\s*{_UNIT}\s+ago\b", re.IGNORECASE)
_CLOCK = r"(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:z|utc)?"
_SINCE_CLOCK = re.compile(rf"\b(?:since|from|starting at|after)\s+{_CLOCK}\b", re.IGNORECASE)
_SINCE_ISO = re.compile(
    r"\bsince\s+(\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)
_BETWEEN = re.compile(rf"\bbetween\s+{_CLOCK}\s+and\s+{_CLOCK}", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}

#: Vendor-neutral symptom vocabulary: tag -> trigger regex on the question.
SYMPTOM_PATTERNS: dict[str, re.Pattern[str]] = {
    "http_5xx": re.compile(r"\b(5\d\d|5xx|internal server error|server errors?)\b", re.I),
    "errors": re.compile(r"\b(errors?|failing|fails|failed|failures?|broken|exceptions?)\b", re.I),
    "latency": re.compile(r"\b(slow|slowness|latency|sluggish|lag+y?|p9[059])\b", re.I),
    "timeouts": re.compile(r"\b(time[ds]?[ -]?outs?|timing out)\b", re.I),
    "unavailable": re.compile(r"\b(503|down|unavailable|outage|unreachable)\b", re.I),
    "restarts": re.compile(r"\b(restart(s|ing)?|crash(es|ing|loop)?|oom\w*|out of memory)\b", re.I),
    "memory": re.compile(r"\b(memory|oom\w*|leak)\b", re.I),
    "deployment": re.compile(r"\b(deploy(ed|ment)?|release[ds]?|rollout|rolled out)\b", re.I),
}

#: Short per-agent objectives; unknown (future) agents fall back to their description.
OBJECTIVES: dict[str, str] = {
    "logs": "Find error patterns, their first occurrence, trace ids and new versions in the logs",
    "metrics": "Compare error rate, latency, saturation and dependency health with the baseline",
    "alerts": "List firing alerts on the service and its dependencies and when they started",
    "k8s": "Check workload health: rollouts, restarts, OOM kills, image pulls, dependencies",
    "code": "Find recent commits, config changes and releases before the incident",
    "knowledge": "Find the runbook sections matching the round-1 symptoms",
    "tickets": "Find known issues and past incidents matching the round-1 symptoms",
}


# --------------------------------------------------------------------------- results


class ParsedQuestion(BaseModel):
    service: str | None = None
    service_source: str = "none"  # explicit | catalog | llm | none
    candidates: list[str] = Field(default_factory=list)
    environment: str | None = None
    time_range: TimeRange
    time_source: str = "default"
    symptoms: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    """What the planner decided. ``steps`` is the round-1 + round-2 DAG."""

    context: IncidentContext
    steps: list[InvestigationStep] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    candidates: list[str] = Field(default_factory=list)
    parsed: ParsedQuestion
    usage: TokenUsage = Field(default_factory=TokenUsage)
    notes: list[str] = Field(default_factory=list)

    def round(self, number: int) -> list[InvestigationStep]:
        return [s for s in self.steps if s.round == number]


class LLMExtraction(BaseModel):
    """What the ``fast`` model may suggest when rules found no service."""

    service: str | None = Field(default=None, description="Service name as written, or null.")
    environment: str | None = None
    since: str | None = Field(default=None, description="Look-back like 30m, 2h, or null.")


# --------------------------------------------------------------------------- parsing


def _ngrams(question: str) -> list[str]:
    words = _WORD.findall(question)
    grams: list[str] = []
    for size in range(MAX_NGRAM, 0, -1):
        for i in range(len(words) - size + 1):
            grams.append("-".join(words[i : i + size]))
    return grams


def find_services(question: str, catalog: ServiceCatalog) -> list[str]:
    """Catalog services named in the question (exact name or alias), in mention order."""
    found: list[tuple[int, str]] = []
    lowered = question.casefold()
    for gram in _ngrams(question):
        key = normalize(gram)
        plural = key[:-1] if key.endswith("s") and len(key) > 3 else None
        for candidate in (key, plural) if plural else (key,):
            resolution = catalog.resolve(candidate)
            if resolution.service is not None and resolution.match in ("exact", "alias"):
                name = resolution.service.name
                if name not in {n for _, n in found}:
                    position = lowered.find(gram.split("-")[0].casefold())
                    found.append((position if position >= 0 else len(lowered), name))
                break
    return [name for _, name in sorted(found)]


def pick_service(names: list[str], catalog: ServiceCatalog) -> str | None:
    """One service, or the caller when every other mentioned service is its dependency."""
    if len(names) == 1:
        return names[0]
    for name in names:
        deps = set(catalog.get(name).depends_on)
        if all(other in deps for other in names if other != name):
            return name
    return None


def find_environment(question: str, catalog: ServiceCatalog) -> str | None:
    for word in _WORD.findall(question):
        env = catalog.resolve_environment(word)
        if env:
            return env
    return None


def _seconds(amount: int, unit: str) -> int:
    return amount * _UNIT_SECONDS[unit[0].casefold()]


def parse_time_range(question: str, now: datetime, default_window: str) -> tuple[TimeRange, str]:
    """(time range, how it was found). Clock times are UTC; a future time means yesterday."""
    match = _BETWEEN.search(question)
    if match:
        start = _clock(now, *match.groups()[0:3])
        end = _clock(now, *match.groups()[3:6])
        if end <= start:
            start -= timedelta(days=1)
        return TimeRange(start=start, end=min(end, now) if end > start else end), "between"
    match = _SINCE_ISO.search(question)
    if match:
        start_ts = parse_timestamp(match.group(1).upper().replace(" ", "T"))
        if start_ts is not None and start_ts < now:
            return TimeRange(start=start_ts, end=now), "since"
    match = _SINCE_CLOCK.search(question)
    if match:
        return TimeRange(start=_clock(now, *match.groups()), end=now), "since"
    match = _LAST.search(question)
    if match:
        amount, unit, bare = match.group(1), match.group(2), match.group(3)
        seconds = _seconds(int(amount), unit) if amount else _seconds(1, bare)
        return TimeRange(start=now - timedelta(seconds=seconds), end=now), "last"
    match = _AGO.search(question)
    if match:
        seconds = _seconds(int(match.group(1)), match.group(2))
        # "started 20 minutes ago": the window starts a little before the reported start.
        start = now - timedelta(seconds=seconds) - timedelta(minutes=5)
        return TimeRange(start=start, end=now), "ago"
    return TimeRange.last(default_window, now=now), "default"


def _clock(now: datetime, hour: str, minute: str, second: str | None) -> datetime:
    at = now.replace(hour=int(hour) % 24, minute=int(minute), second=int(second or 0))
    at = at.replace(microsecond=0)
    return at - timedelta(days=1) if at >= now else at


def find_symptoms(question: str) -> list[str]:
    return [tag for tag, pattern in SYMPTOM_PATTERNS.items() if pattern.search(question)]


# --------------------------------------------------------------------------- planner


@dataclass
class PlanRequest:
    question: str
    service: str | None = None
    environment: str | None = None
    since: str | None = None  # "30m"
    start: datetime | None = None
    end: datetime | None = None
    now: datetime | None = None
    hints: dict[str, object] = field(default_factory=dict)


class Planner:
    def __init__(
        self,
        settings: Settings,
        catalog: ServiceCatalog,
        registry: AgentRegistry,
        llm: LLMProvider | None = None,
    ) -> None:
        self.settings = settings
        self.catalog = catalog
        self.registry = registry
        self.llm = llm

    # -- agents ------------------------------------------------------------------------------

    def enabled_agents(self) -> list[AgentSpec]:
        """Registered agents whose capabilities are all enabled in the profile."""
        specs = []
        for spec in self.registry.specs():
            if not spec.capabilities or not self.settings.agent(spec.name).enabled:
                continue
            caps = [self.settings.capabilities.get(c) for c in spec.capabilities]
            if all(cap is not None and cap.enabled for cap in caps):
                specs.append(spec)
        return specs

    # -- parsing -----------------------------------------------------------------------------

    async def parse(self, request: PlanRequest) -> tuple[ParsedQuestion, TokenUsage, list[str]]:
        now = request.end or request.now or utcnow()
        usage = TokenUsage()
        notes: list[str] = []
        config = self.settings.orchestrator

        # Service
        service: str | None = None
        source = "none"
        candidates: list[str] = []
        if request.service:
            resolution = self.catalog.resolve(request.service)
            if resolution.service is not None:
                service, source = resolution.service.name, "explicit"
            else:
                candidates = resolution.candidates or [s.name for s in self.catalog.services]
                notes.append(f"'{request.service}' is not in the service catalog")
        else:
            names = find_services(request.question, self.catalog)
            service = pick_service(names, self.catalog)
            if service:
                source = "catalog"
            elif names:
                candidates = names

        # Environment
        environment = (
            self.catalog.resolve_environment(request.environment)
            if request.environment
            else find_environment(request.question, self.catalog)
        )

        # Time range
        if request.start is not None:
            time_range, time_source = TimeRange(start=request.start, end=now), "explicit"
        elif request.since:
            time_range = TimeRange(start=now - parse_duration(request.since), end=now)
            time_source = "explicit"
        else:
            time_range, time_source = parse_time_range(request.question, now, config.default_window)

        # LLM fallback: only for what the rules couldn't find, and only for a service that
        # resolves through the catalog (never invented).
        if (
            service is None
            and not candidates
            and not request.service
            and config.llm_planner_fallback
        ):
            extraction, usage = await self._llm_extract(request.question, notes)
            if extraction and extraction.service:
                resolution = self.catalog.resolve(extraction.service)
                if resolution.service is not None:
                    service, source = resolution.service.name, "llm"
                else:
                    candidates = resolution.candidates
                    notes.append(f"LLM suggested '{extraction.service}', not in the catalog")
            if extraction and not environment and extraction.environment:
                environment = self.catalog.resolve_environment(extraction.environment)
            if extraction and time_source == "default" and extraction.since:
                try:
                    time_range = TimeRange(start=now - parse_duration(extraction.since), end=now)
                    time_source = "llm"
                except ValueError:
                    notes.append(f"LLM time range '{extraction.since}' ignored")

        if environment is None and self.catalog.environments:
            environment = self.catalog.environments[0]
        parsed = ParsedQuestion(
            service=service,
            service_source=source,
            candidates=candidates,
            environment=environment,
            time_range=time_range,
            time_source=time_source,
            symptoms=find_symptoms(request.question),
        )
        return parsed, usage, notes

    async def _llm_extract(
        self, question: str, notes: list[str]
    ) -> tuple[LLMExtraction | None, TokenUsage]:
        if self.llm is None:
            return None, TokenUsage()
        services = ", ".join(
            f"{s.name} (aliases: {', '.join(s.aliases) or '-'})" for s in self.catalog.services
        )
        messages = [
            ChatMessage.system(
                "Extract the service, environment and look-back window from an incident "
                "question. Only answer with a service from this list, or null: "
                f"{services}. Environments: {', '.join(self.catalog.environments)}."
            ),
            ChatMessage.user(question),
        ]
        try:
            extraction, usage = await generate_structured(
                self.llm, messages, LLMExtraction, role="fast", max_attempts=1
            )
        except LLMError as exc:
            notes.append(f"LLM planner fallback unavailable: {exc}")
            return None, TokenUsage()
        return extraction, usage

    # -- planning ----------------------------------------------------------------------------

    def clarification(self, parsed: ParsedQuestion) -> tuple[str, list[str]]:
        candidates = parsed.candidates or [s.name for s in self.catalog.services]
        if parsed.candidates:
            question = (
                f"Which service is affected: {', '.join(candidates)}? "
                "And since when (e.g. 'last 30m' or 'since 10:15')?"
            )
        else:
            question = (
                "Which service is affected, and since when? Known services: "
                f"{', '.join(candidates)}."
            )
        return question, candidates

    def steps(self, parsed: ParsedQuestion) -> list[InvestigationStep]:
        round2 = set(self.settings.orchestrator.round2_agents)
        max_rounds = self.settings.orchestrator.max_rounds
        first: list[InvestigationStep] = []
        second: list[InvestigationStep] = []
        for spec in self.enabled_agents():
            objective = self.objective(spec, parsed.service)
            if spec.name in round2 and max_rounds >= 2:
                second.append(InvestigationStep(agent=spec.name, objective=objective, round=2))
            else:
                first.append(InvestigationStep(agent=spec.name, objective=objective, round=1))
        ids = [s.id for s in first]
        second = [s.model_copy(update={"depends_on": ids}) for s in second]
        return [*first, *second]

    @staticmethod
    def objective(spec: AgentSpec, service: str | None, extra: str = "") -> str:
        base = OBJECTIVES.get(spec.name, spec.description.rstrip("."))
        text = f"{base} for {service}" if service else base
        return f"{text}: {extra}" if extra else text

    async def plan(self, request: PlanRequest) -> Plan:
        parsed, usage, notes = await self.parse(request)
        context = IncidentContext(
            question=request.question,
            service=parsed.service,
            environment=parsed.environment,
            time_range=parsed.time_range,
            symptoms=list(parsed.symptoms),
        )
        if parsed.service is None:
            question, candidates = self.clarification(parsed)
            return Plan(
                context=context,
                needs_clarification=True,
                clarification_question=question,
                candidates=candidates,
                parsed=parsed,
                usage=usage,
                notes=notes,
            )
        return Plan(
            context=context, steps=self.steps(parsed), parsed=parsed, usage=usage, notes=notes
        )


def service_entry(catalog: ServiceCatalog, name: str | None) -> ServiceEntry | None:
    if not name:
        return None
    resolution = catalog.resolve(name)
    return resolution.service
