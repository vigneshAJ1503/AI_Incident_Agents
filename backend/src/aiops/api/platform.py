"""Platform questions (PR-041): answers from live platform data, no investigation started.

``POST /api/ask`` classifies the question (``aiops.orchestrator.intent``); a platform intent
is answered here from the runner (in-process investigations + their event log), the
evidence store, the agent registry, the profile and the cached capability health. The
builders are shared with ``GET /health`` and ``GET /agents`` so both always agree.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from aiops import __version__
from aiops.agents.registry import AGENTS
from aiops.api import models as m
from aiops.api.context import ApiContext
from aiops.api.health import llm_configured, llm_missing
from aiops.api.scenarios import faults_enabled
from aiops.core.events import InvestigationEvent
from aiops.core.models import Investigation, InvestigationStatus, utcnow
from aiops.orchestrator.dashboard import agent_stats
from aiops.orchestrator.intent import Classification

#: Upper bound of investigations loaded for the agent statistics.
STATS_LIMIT = 5_000
#: Example incident questions when live mode is available (no recorded scenario needed).
LIVE_EXAMPLES = [
    ("Payment API is returning HTTP 500 in production", "errors on one service"),
    ("Why are orders slow since 10:15?", "latency, with a time"),
    ("order-service pods keep restarting", "Kubernetes"),
    ("Did the last deploy of user-service break login?", "a suspected release"),
]
PLATFORM_EXAMPLES = [
    ("What agents are running now?", "live status"),
    ("Which agents do you have?", "the agent catalog"),
    ("Is the LLM configured?", "platform health"),
    ("What happened with the last payment incident?", "past investigations"),
]
OPEN = {InvestigationStatus.PENDING, InvestigationStatus.RUNNING}


def href(inv_id: str) -> str:
    return f"/investigations/{inv_id}"


# --------------------------------------------------------------------------- shared builders


async def health_of(ctx: ApiContext) -> m.HealthResponse:
    """``GET /health``: profile, LLM (never the key), capabilities (cached TCP), store."""
    store_ok = True
    try:
        async with asyncio.timeout(max(ctx.settings.api.health_timeout_s, 2.0)):
            await ctx.store.count()
    except Exception:
        store_ok = False
    llm = ctx.settings.llm
    return m.HealthResponse(
        status="ok" if store_ok else "degraded",
        version=__version__,
        profile=ctx.settings.profile,
        llm=m.LLMHealth(provider=llm.provider, configured=llm_configured(ctx.settings)),
        capabilities=await ctx.health.status(),  # type: ignore[arg-type]
        faults_enabled=faults_enabled(),
        store="ok" if store_ok else "down",
    )


async def agents_of(ctx: ApiContext) -> list[m.AgentOut]:
    """``GET /agents``: the registry + 7-day statistics from the evidence store."""
    recent = await ctx.store.search(since=utcnow() - timedelta(days=7), limit=STATS_LIMIT)
    stats = {a["name"]: a for a in agent_stats(recent)}
    enabled = {n for n, c in ctx.settings.capabilities.items() if c.enabled}
    out = []
    for spec in AGENTS.specs():
        stat = stats.get(spec.name, {})
        out.append(
            m.AgentOut(
                name=spec.name,
                version=spec.version,
                description=spec.description,
                capabilities=list(spec.capabilities),
                last_run_at=stat.get("last_run_at"),
                success_rate_7d=stat.get("success_rate"),
                p50_ms=stat.get("p50_ms"),
                runs_7d=stat.get("runs", 0),
                enabled=ctx.settings.agent(spec.name).enabled and set(spec.capabilities) <= enabled,
            )
        )
    return out


def root_cause_statement(inv: Investigation) -> str | None:
    """The RCA's root cause (else its summary)."""
    report = inv.report
    if report is None:
        return None
    top = next((h for h in inv.hypotheses if h.id == report.root_cause_hypothesis_id), None)
    return top.statement if top else (report.summary or None)


async def replay_reasons(ctx: ApiContext) -> tuple[list[str], bool]:
    """Why investigations can't run live (``[]`` = they can) and whether the LLM is set up.
    Precise: the LLM's missing settings and/or the unreachable capabilities by name."""
    llm_ok = llm_configured(ctx.settings)
    reasons = []
    if not llm_ok:
        reasons.append("no LLM is configured (" + "; ".join(llm_missing(ctx.settings)) + ")")
    caps = await ctx.health.status()
    down = sorted(n for n, s in caps.items() if s == "down")
    if down:
        what = "capability is" if len(down) == 1 else "capabilities are"
        reasons.append(f"the {what} unreachable: {', '.join(down)}")
    return reasons, llm_ok


def scenario_suggestions(ctx: ApiContext) -> list[m.SuggestionItem]:
    return [
        m.SuggestionItem(
            question=s.question, hint=f"{s.id} · {s.service or 'any service'}", scenario=s.id
        )
        for s in ctx.scenarios.scenarios
        if s.question
    ]


# --------------------------------------------------------------------------- answers


def _item(inv: Investigation) -> m.InvestigationItem:
    report = inv.report
    return m.InvestigationItem(
        id=inv.id,
        title=(inv.context.question if inv.context else None) or inv.incident.title,
        service=(inv.context.service if inv.context else None) or inv.incident.service,
        status=inv.status,
        severity=report.severity if report else None,
        root_cause=root_cause_statement(inv),
        confidence=report.confidence if report else None,
        created_at=inv.created_at,
        href=href(inv.id),
    )


def _running_item(
    inv: Investigation, events: list[InvestigationEvent]
) -> m.RunningInvestigationItem:
    last_tool: dict[str, str] = {}
    round_no: int | None = None
    for event in events:
        if event.type == "tool_called" and event.data.get("step_id"):
            last_tool[str(event.data["step_id"])] = str(event.data.get("tool", ""))
        elif event.type == "round_started":
            round_no = int(event.data.get("round", 0)) or round_no
    running_rounds = [s.round for s in inv.steps if s.status.value == "running"]
    if running_rounds:
        round_no = max(running_rounds)
    agents = [
        m.RunningAgent(
            agent=s.agent, round=s.round, status=s.status.value, tool=last_tool.get(s.id) or None
        )
        for s in inv.steps
    ]
    return m.RunningInvestigationItem(
        id=inv.id,
        question=(inv.context.question if inv.context else None) or inv.incident.title,
        service=(inv.context.service if inv.context else None) or inv.incident.service,
        status=inv.status,
        mode=inv.mode,
        created_at=inv.created_at,
        elapsed_s=round(max((utcnow() - inv.created_at).total_seconds(), 0.0), 1),
        round=round_no,
        agents=agents,
        href=href(inv.id),
    )


async def running_investigations(ctx: ApiContext) -> list[m.RunningInvestigationItem]:
    """In-process runs (``Orchestrator.running()`` via the runner, events from the bus) plus
    the store's pending/running ones (another API process), newest first."""
    items: dict[str, m.RunningInvestigationItem] = {}
    for inv_id in ctx.runner.active_ids():
        inv = ctx.runner.snapshot(inv_id)
        if inv is not None:
            items[inv_id] = _running_item(inv, ctx.bus.history(inv_id))
    for status in OPEN:
        for inv in await ctx.store.search(status=status.value, limit=20):
            if inv.id not in items:
                items[inv.id] = _running_item(inv, await ctx.store.events(inv.id))
    return sorted(items.values(), key=lambda i: i.created_at, reverse=True)


async def answer_running(ctx: ApiContext, c: Classification) -> m.AskAnswer:
    running = await running_investigations(ctx)
    if running:
        lines = []
        for r in running:
            active = [a.agent for a in r.agents if a.status == "running"]
            doing = f"agents working: {', '.join(active)}" if active else r.status.value
            lines.append(
                f"- **{r.question}** ({r.service or 'service not resolved yet'}): "
                f"{doing}, round {r.round or '-'}, {int(r.elapsed_s)} s so far"
            )
        n = len(running)
        return m.AskAnswer(
            title=f"{n} investigation{'s' if n != 1 else ''} running",
            markdown="\n".join(lines),
            items=list(running),
            links=[m.AskLink(label="Open live view", href=running[0].href)],
        )
    recent = await ctx.store.search(limit=3)
    return m.AskAnswer(
        title="Nothing is running right now",
        markdown="No investigation is running, so no agent is working. "
        + ("The last ones:" if recent else "No investigation has run yet."),
        items=[_item(i) for i in recent],
        links=[m.AskLink(label="All investigations", href="/investigations")],
    )


async def answer_catalog(ctx: ApiContext, c: Classification) -> m.AskAnswer:
    agents = await agents_of(ctx)
    caps = ctx.settings.capabilities
    items = [
        m.AgentItem(
            name=a.name,
            description=a.description,
            capabilities=a.capabilities,
            providers=[caps[n].provider for n in a.capabilities if n in caps],
            enabled=a.enabled,
            success_rate_7d=a.success_rate_7d,
            p50_ms=a.p50_ms,
            runs_7d=a.runs_7d,
        )
        for a in agents
    ]
    on = [a for a in items if a.enabled]
    markdown = (
        f"**{len(items)} specialist agents** ({len(on)} enabled in profile "
        f"`{ctx.settings.profile}`) check an incident in parallel; an RCA agent then ranks "
        "the hypotheses with evidence. Each agent binds to a capability, and the profile "
        "picks the provider behind it."
    )
    return m.AskAnswer(
        title="The agents",
        markdown=markdown,
        items=list(items),
        links=[m.AskLink(label="Agents page", href="/agents")],
    )


async def answer_health(ctx: ApiContext, c: Classification) -> m.AskAnswer:
    health = await health_of(ctx)
    reasons, llm_ok = await replay_reasons(ctx)
    llm = ctx.settings.llm
    checks: list[m.CheckItem] = [
        m.CheckItem(
            name="LLM",
            status="ok" if llm_ok else "down",
            detail=f"{llm.provider}: configured"
            if llm_ok
            else "; ".join(llm_missing(ctx.settings)),
        )
    ]
    caps = ctx.settings.capabilities
    for name, state in sorted(health.capabilities.items()):
        provider = caps[name].provider if name in caps else "?"
        checks.append(
            m.CheckItem(
                name=name,
                status=state,
                detail=f"{provider}: "
                + {"ok": "reachable", "down": "unreachable", "disabled": "disabled in the profile"}[
                    state
                ],
            )
        )
    checks.append(
        m.CheckItem(
            name="evidence store",
            status="ok" if health.store == "ok" else "down",
            detail="Postgres" if health.store == "ok" else "unreachable",
        )
    )
    checks.append(
        m.CheckItem(
            name="fault injection",
            status="info",
            detail="enabled" if health.faults_enabled else "disabled (AIOPS_ENABLE_FAULTS)",
        )
    )
    checks.append(m.CheckItem(name="profile", status="info", detail=health.profile))
    down = [ch.name for ch in checks if ch.status == "down"]
    summary = "Everything is up." if not down else f"Down or not configured: **{', '.join(down)}**."
    mode = (
        "New investigations run **live** against the real data sources."
        if not reasons
        else "New investigations run in **replay mode** (recorded scenarios) because "
        + " and ".join(reasons)
        + "."
    )
    return m.AskAnswer(
        title="Platform health",
        markdown=f"{summary}\n\n{mode}",
        items=list(checks),
        links=[m.AskLink(label="Dashboard", href="/")],
    )


async def answer_recent(ctx: ApiContext, c: Classification, question: str) -> m.AskAnswer:
    words = question.casefold()
    status = next(
        (s.value for s in InvestigationStatus if s.value.replace("_", " ") in words), None
    )
    severity = next((s for s in ("critical", "high", "medium", "low") if s in words), None)
    single = any(w in words for w in ("the last ", "the latest ", "most recent "))
    found = await ctx.store.search(
        service=c.service, status=status, severity=severity, limit=1 if single else 5
    )
    filters = ", ".join(x for x in (c.service, status, severity) if x)
    title = "Recent investigations" + (f" ({filters})" if filters else "")
    if not found:
        return m.AskAnswer(
            title=title,
            markdown="No investigation matches" + (f" {filters}." if filters else "."),
            links=[m.AskLink(label="All investigations", href="/investigations")],
        )
    lines = []
    for inv in found:
        rc = root_cause_statement(inv) or inv.status.value
        conf = f" ({inv.report.confidence:.0%} confidence)" if inv.report else ""
        lines.append(f"- **{_item(inv).title}**: {rc}{conf}")
    query = f"?service={c.service}" if c.service else ""
    return m.AskAnswer(
        title=title,
        markdown="\n".join(lines),
        items=[_item(i) for i in found],
        links=[m.AskLink(label="Investigations", href=f"/investigations{query}")],
    )


async def answer_help(ctx: ApiContext, c: Classification) -> m.AskAnswer:
    reasons, _ = await replay_reasons(ctx)
    enabled = [
        f"{n} ({cap.provider})"
        for n, cap in sorted(ctx.settings.capabilities.items())
        if cap.enabled
    ]
    if reasons:
        incident = scenario_suggestions(ctx)
        how = (
            "Right now investigations run in **replay mode** (" + " and ".join(reasons) + "), so "
            "ask one of the recorded incidents below."
        )
    else:
        incident = [m.SuggestionItem(question=q, hint=h) for q, h in LIVE_EXAMPLES]
        how = "Describe the problem in plain language: the service, the symptom, and since when."
    platform = [m.SuggestionItem(question=q, hint=h) for q, h in PLATFORM_EXAMPLES]
    markdown = (
        f"Ask about an **incident** and the agents investigate it. {how}\n\n"
        f"Or ask about the **platform** itself (agents, health, past incidents).\n\n"
        f"Capabilities: {', '.join(enabled) or 'none enabled'}."
    )
    return m.AskAnswer(
        title="What you can ask",
        markdown=markdown,
        items=[*incident, *platform],
        links=[m.AskLink(label="Scenarios", href="/scenarios")],
    )


Builder = Callable[[ApiContext, Classification, str], Awaitable[m.AskAnswer]]
ANSWERS: dict[str, Builder] = {
    "agents_running": lambda ctx, c, q: answer_running(ctx, c),
    "agents_catalog": lambda ctx, c, q: answer_catalog(ctx, c),
    "health": lambda ctx, c, q: answer_health(ctx, c),
    "recent_investigations": answer_recent,
    "help": lambda ctx, c, q: answer_help(ctx, c),
}


async def answer(ctx: ApiContext, c: Classification, question: str) -> m.AskAnswer:
    return await ANSWERS[c.intent](ctx, c, question)
