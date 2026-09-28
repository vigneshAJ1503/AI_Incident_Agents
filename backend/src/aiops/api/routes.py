"""The endpoints of docs/api/contract.md (all under ``/api``)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from aiops import __version__
from aiops.agents.registry import AGENTS
from aiops.agents.tickets_agent.draft import ACTIONS, draft_ticket
from aiops.api import models as m
from aiops.api.context import ApiContext
from aiops.api.errors import ApiError, not_found
from aiops.api.health import llm_configured
from aiops.api.runner import RunMode
from aiops.api.scenarios import faults_enabled, injectable
from aiops.api.sse import event_stream
from aiops.core.guardrails.approvals import (
    ActionProposal,
    ActionStatus,
    ApprovalError,
    Risk,
)
from aiops.core.models import Investigation, InvestigationStatus, utcnow
from aiops.evals.scenario import Scenario
from aiops.faults import FaultError
from aiops.mcp import tickets
from aiops.orchestrator.dashboard import agent_stats, dashboard_summary
from aiops.orchestrator.engine import InvestigationRequest
from aiops.store.repository import summary_of

log = logging.getLogger("aiops.api")
router = APIRouter(prefix="/api")

#: Upper bound of investigations loaded for the dashboard/agent statistics.
STATS_LIMIT = 5_000


def get_ctx(request: Request) -> ApiContext:
    ctx: ApiContext = request.app.state.ctx
    return ctx


Ctx = Annotated[ApiContext, Depends(get_ctx)]
ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": m.ErrorResponse} for code in (400, 401, 403, 404, 409, 422, 429, 503)
}


# --------------------------------------------------------------------------- health + catalog


@router.get("/health", response_model=m.HealthResponse, tags=["meta"])
async def health(ctx: Ctx) -> m.HealthResponse:
    """Profile, LLM (never the key), capability reachability (cached TCP checks), store."""
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


@router.get("/services", response_model=list[m.ServiceOut], tags=["catalog"])
async def services(ctx: Ctx) -> list[m.ServiceOut]:
    return [
        m.ServiceOut(
            name=s.name,
            description=s.description,
            owners=s.owners,
            depends_on=s.depends_on,
            environments=sorted(s.environments) or ctx.catalog.environments,
            runbooks=s.runbooks,
        )
        for s in ctx.catalog.services
    ]


@router.get("/agents", response_model=list[m.AgentOut], tags=["catalog"])
async def agents(ctx: Ctx) -> list[m.AgentOut]:
    """The agent registry + 7-day statistics from the evidence store."""
    since = utcnow() - timedelta(days=7)
    recent = await ctx.store.search(since=since, limit=STATS_LIMIT)
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


@router.get("/dashboard/summary", response_model=m.DashboardSummary, tags=["dashboard"])
async def dashboard(ctx: Ctx, days: Annotated[int, Query(ge=1, le=90)] = 14) -> dict[str, Any]:
    now = utcnow()
    window = await ctx.store.search(since=now - timedelta(days=days), limit=STATS_LIMIT)
    return dashboard_summary(window, days=days, now=now)


# --------------------------------------------------------------------------- investigations


def encode_cursor(inv: Investigation) -> str:
    raw = f"{inv.created_at.isoformat()}|{inv.id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        stamp, inv_id = raw.split("|", 1)
        created = datetime.fromisoformat(stamp)
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ApiError(400, "invalid_cursor", "The cursor is not valid; start again.") from exc
    return (created if created.tzinfo else created.replace(tzinfo=UTC)), inv_id


@router.get(
    "/investigations", response_model=m.InvestigationPage, responses=ERRORS, tags=["investigations"]
)
async def list_investigations(
    ctx: Ctx,
    status: InvestigationStatus | None = None,
    service: str | None = None,
    severity: m.Severity | None = None,
    mode: Annotated[str | None, Query(pattern="^(live|replay|demo)$")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Newest first, keyset pagination (``next_cursor`` is opaque)."""
    before, before_id = decode_cursor(cursor) if cursor else (None, None)
    if service:
        resolved = ctx.catalog.resolve(service).service
        service = resolved.name if resolved else service
    items = await ctx.store.search(
        status=status.value if status else None,
        service=service,
        severity=severity,
        mode=mode,
        q=q.strip() if q else None,
        limit=limit + 1,
        before=before,
        before_id=before_id,
    )
    page = items[:limit]
    return {
        "items": [summary_of(i) for i in page],
        "next_cursor": encode_cursor(page[-1]) if len(items) > limit else None,
    }


async def _choose_mode(
    ctx: ApiContext, body: m.CreateInvestigation
) -> tuple[RunMode, Scenario | None]:
    """``live`` when an LLM key is configured and every capability is reachable, else a
    ``replay`` of the scenario named (``scenario``) or matching the question/service."""
    if body.scenario:
        scenario = ctx.scenarios.get(body.scenario)
        if scenario is None:
            known = ", ".join(s.id for s in ctx.scenarios.scenarios) or "none"
            raise ApiError(404, "unknown_scenario", f"No scenario '{body.scenario}' ({known}).")
        if body.mode == "live":
            raise ApiError(422, "validation_error", "A scenario is replayed: use mode 'replay'.")
        return "replay", scenario
    llm_ok = llm_configured(ctx.settings)
    caps = await ctx.health.status()
    down = sorted(n for n, s in caps.items() if s == "down")
    live_ok = llm_ok and not down
    if body.mode == "live" and not live_ok:
        reasons = []
        if not llm_ok:
            reasons.append("no LLM configured (OPENAI_COMPAT_API_KEY, LLM_MODEL_AGENT)")
        if down:
            reasons.append(f"capabilities down: {', '.join(down)}")
        raise ApiError(
            409,
            "live_unavailable",
            f"Live mode is unavailable: {'; '.join(reasons)}. Use mode 'replay' with a scenario.",
        )
    if live_ok and body.mode != "replay":
        return "live", None
    scenario = ctx.scenarios.match(body.question, body.service, ctx.catalog)
    if scenario is None:
        known = "; ".join(f"{s.id} ({s.service}): {s.question}" for s in ctx.scenarios.scenarios)
        why = (
            "Replay mode"
            if body.mode == "replay" or live_ok
            else ("No LLM configured or a capability is down, so this runs in replay mode")
        )
        raise ApiError(
            422,
            "no_matching_scenario",
            f"{why}, which needs a recorded scenario: pass 'scenario' or ask about a scenario's "
            f"service. Known: {known or 'none'}.",
        )
    return "replay", scenario


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@router.post(
    "/investigations",
    status_code=202,
    response_model=m.CreatedInvestigation,
    responses=ERRORS,
    tags=["investigations"],
)
async def create_investigation(ctx: Ctx, body: m.CreateInvestigation) -> m.CreatedInvestigation:
    """Start an investigation in the background; follow it on ``/events``."""
    mode, scenario = await _choose_mode(ctx, body)
    request = InvestigationRequest(
        question=body.question,
        service=body.service,
        environment=body.environment,
        since=body.since,
        start=_aware(body.start),
        end=_aware(body.end),
        mode=mode,
    )
    handle = await ctx.runner.start(request, scenario=scenario.id if scenario else None)
    return m.CreatedInvestigation(
        id=handle.id, status=InvestigationStatus.PENDING, mode=mode, scenario=handle.scenario
    )


async def _investigation(ctx: ApiContext, investigation_id: str) -> Investigation:
    inv = ctx.runner.snapshot(investigation_id) or await ctx.store.get(investigation_id)
    if inv is None:
        raise not_found("Investigation", investigation_id)
    return inv


@router.get(
    "/investigations/{investigation_id}",
    response_model=Investigation,
    responses=ERRORS,
    tags=["investigations"],
)
async def get_investigation(ctx: Ctx, investigation_id: str) -> Investigation:
    return await _investigation(ctx, investigation_id)


@router.get(
    "/investigations/{investigation_id}/report.md",
    response_class=PlainTextResponse,
    responses={**ERRORS, 200: {"content": {"text/markdown": {}}}},
    tags=["investigations"],
)
async def report_markdown(ctx: Ctx, investigation_id: str) -> PlainTextResponse:
    inv = await _investigation(ctx, investigation_id)
    if inv.report is None:
        raise ApiError(
            409, "report_not_ready", f"Investigation '{investigation_id}' has no report yet."
        )
    return PlainTextResponse(
        inv.report.markdown or inv.report.summary,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'inline; filename="{investigation_id}.md"'},
    )


@router.get(
    "/investigations/{investigation_id}/events",
    response_class=StreamingResponse,
    responses={**ERRORS, 200: {"content": {"text/event-stream": {}}}},
    tags=["investigations"],
)
async def events(
    ctx: Ctx,
    investigation_id: str,
    last_event_id_header: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    last_event_id: Annotated[int | None, Query(ge=0)] = None,
) -> StreamingResponse:
    """SSE: stored events after ``Last-Event-ID`` / ``?last_event_id=``, then live ones;
    a ``heartbeat`` every ``api.heartbeat_s``; ends after ``investigation_finished``."""
    if not ctx.runner.is_live(investigation_id) and await ctx.store.get(investigation_id) is None:
        raise not_found("Investigation", investigation_id)
    after = last_event_id or 0
    if last_event_id_header and last_event_id_header.strip().isdigit():
        after = max(after, int(last_event_id_header.strip()))
    stream = event_stream(
        investigation_id,
        after=after,
        store=ctx.store,
        bus=ctx.bus,
        is_live=ctx.runner.is_live,
        heartbeat_s=ctx.settings.api.heartbeat_s,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post(
    "/investigations/{investigation_id}/clarify",
    status_code=202,
    response_model=m.CreatedInvestigation,
    responses=ERRORS,
    tags=["investigations"],
)
async def clarify(
    ctx: Ctx, investigation_id: str, body: m.ClarifyRequest
) -> m.CreatedInvestigation:
    """Resume a ``needs_clarification`` investigation with the engineer's answer (usually
    one of ``clarification_candidates``). Events continue on the same stream (``seq``)."""
    if ctx.runner.is_live(investigation_id):
        raise ApiError(409, "already_running", f"Investigation '{investigation_id}' is running.")
    inv = await _investigation(ctx, investigation_id)
    if inv.status is not InvestigationStatus.NEEDS_CLARIFICATION:
        raise ApiError(
            409,
            "not_waiting_for_clarification",
            f"Investigation '{investigation_id}' is {inv.status.value}, not needs_clarification.",
        )
    question = inv.context.question if inv.context else inv.incident.title
    resolved = ctx.catalog.resolve(body.answer).service
    service = resolved.name if resolved else None
    if service is None:
        question = f"{question} ({body.answer})"
    create = m.CreateInvestigation(
        question=question,
        service=service,
        environment=inv.context.environment if inv.context else None,
    )
    mode, scenario = await _choose_mode(ctx, create)
    request = InvestigationRequest(
        question=question,
        service=service,
        environment=create.environment,
        start=inv.context.time_range.start if inv.context else None,
        end=inv.context.time_range.end if inv.context else None,
        mode=mode,
    )
    handle = await ctx.runner.start(
        request,
        scenario=scenario.id if scenario else None,
        investigation_id=investigation_id,
        created_at=inv.created_at,
        history=await ctx.store.events(investigation_id),
    )
    return m.CreatedInvestigation(
        id=handle.id, status=InvestigationStatus.RUNNING, mode=mode, scenario=handle.scenario
    )


@router.post(
    "/investigations/{investigation_id}/cancel",
    status_code=202,
    response_model=m.InvestigationState,
    responses=ERRORS,
    tags=["investigations"],
)
async def cancel(ctx: Ctx, investigation_id: str) -> m.InvestigationState:
    """Cancel a running investigation (it finishes with status ``cancelled``)."""
    if ctx.runner.cancel(investigation_id):
        return m.InvestigationState(id=investigation_id, status=InvestigationStatus.CANCELLED)
    inv = await _investigation(ctx, investigation_id)
    open_states = {
        InvestigationStatus.PENDING,
        InvestigationStatus.RUNNING,
        InvestigationStatus.NEEDS_CLARIFICATION,
    }
    if inv.status not in open_states:
        raise ApiError(
            409, "not_running", f"Investigation '{investigation_id}' is {inv.status.value}."
        )
    # Waiting for a clarification (or orphaned by a restart): nothing runs, just close it.
    closed = inv.model_copy(
        update={"status": InvestigationStatus.CANCELLED, "completed_at": utcnow()}
    )
    await ctx.store.save(closed)
    return m.InvestigationState(id=investigation_id, status=InvestigationStatus.CANCELLED)


@router.post(
    "/investigations/{investigation_id}/tickets/draft",
    status_code=201,
    response_model=m.TicketDraftResponse,
    responses=ERRORS,
    tags=["approvals"],
)
async def draft_ticket_proposal(
    ctx: Ctx, investigation_id: str, body: m.TicketDraftRequest | None = None
) -> m.TicketDraftResponse:
    """Draft a ticket from the findings as an approval PROPOSAL; nothing is written until
    a human approves it (``POST /approvals/{id}/approve``)."""
    body = body or m.TicketDraftRequest()
    inv = await _investigation(ctx, investigation_id)
    if not inv.results:
        raise ApiError(
            409, "nothing_to_draft", f"Investigation '{investigation_id}' has no findings yet."
        )
    service_name = (inv.context.service if inv.context else None) or inv.incident.service
    labels: list[str] = []
    components: list[str] = []
    if service_name:
        resolved = ctx.catalog.resolve(service_name).service
        if resolved is not None:
            ids = resolved.identifiers("tickets")
            labels = [str(v) for v in ids.get("labels", [])]
            components = [str(v) for v in ids.get("components", [])]
    ticket = draft_ticket(
        inv.results,
        service=service_name,
        labels=labels,
        components=components,
        investigation_id=inv.id,
        issue_type=body.issue_type,
    )
    try:
        cap = ctx.settings.capability("tickets")
    except Exception as exc:
        raise ApiError(409, "capability_disabled", str(exc)) from exc
    project = str(cap.settings.get("project_key", "OPS"))
    risk: Risk
    if body.comment_on:
        tool, arguments = tickets.ADD_COMMENT, ticket.comment_arguments(body.comment_on)
        reason, risk = f"Add investigation findings to {body.comment_on}", "low"
    else:
        tool, arguments = tickets.CREATE_ISSUE, ticket.create_arguments(project)
        reason = f"Create a {body.issue_type} in {project} for: {ticket.summary}"
        risk = "medium"
    proposal = await asyncio.to_thread(
        ctx.approvals.propose,
        action=ACTIONS[tool],
        capability="tickets",
        tool=tool,
        arguments=arguments,
        reason=reason,
        requested_by=body.requested_by,
        risk=risk,
        investigation_id=inv.id,
    )
    if proposal.status is ActionStatus.REJECTED:
        raise ApiError(422, "policy_rejected", proposal.policy_reason or "rejected by policy")
    try:
        await ctx.store.link_approval(proposal.id, inv.id, proposal.action, proposal.created_at)
    except Exception as exc:  # the link is a convenience for queries; the proposal is stored
        log.warning("could not link approval %s to %s: %s", proposal.id, inv.id, exc)
    if ctx.runner.is_live(inv.id):
        ctx.bus.publish(
            "approval_requested", inv.id, approval_id=proposal.id, action=proposal.action
        )
    return m.TicketDraftResponse(
        approval_id=proposal.id, status=_approval_state(proposal.status), summary=ticket.summary
    )


# --------------------------------------------------------------------------- approvals


def _approval_state(status: ActionStatus) -> Any:
    return {ActionStatus.REJECTED: "denied", ActionStatus.EXPIRED: "denied"}.get(
        status, status.value
    )


def approval_out(p: ActionProposal) -> m.ApprovalOut:
    return m.ApprovalOut(
        id=p.id,
        action=p.action,
        capability=p.capability,
        tool=p.tool,
        arguments=p.arguments,
        reason=p.reason,
        risk=p.risk,
        status=_approval_state(p.status),
        lifecycle_status=p.status.value,
        requested_by=p.requested_by,
        investigation_id=p.investigation_id,
        created_at=p.created_at,
        expires_at=p.expires_at,
        decided_by=p.decided_by,
        decided_at=p.decided_at,
        comment=p.decision_note,
        result=p.result,
        error=p.error,
        policy_reason=p.policy_reason,
    )


def _approval_error(exc: ApprovalError) -> ApiError:
    message = str(exc)
    if message.startswith("No action proposal"):
        return ApiError(404, "not_found", message)
    return ApiError(409, "invalid_state", message)


@router.get("/approvals", response_model=list[m.ApprovalOut], responses=ERRORS, tags=["approvals"])
async def list_approvals(ctx: Ctx, status: ActionStatus | None = None) -> list[m.ApprovalOut]:
    """Newest first (expired proposals are marked on the way)."""
    items = await asyncio.to_thread(ctx.approvals.proposals, status)
    return [approval_out(p) for p in items]


@router.post(
    "/approvals/{approval_id}/approve",
    response_model=m.ApprovalOut,
    responses=ERRORS,
    tags=["approvals"],
)
async def approve(ctx: Ctx, approval_id: str, body: m.DecisionRequest) -> m.ApprovalOut:
    """Approve and EXECUTE through ``ApprovalExecutor`` (the only write path). A failed
    execution is returned with ``status: failed`` and ``error``."""
    try:
        await asyncio.to_thread(ctx.approvals.approve, approval_id, body.by, body.comment or "")
        executor = ctx.executor_factory(ctx.approvals)
        proposal = await executor.execute(approval_id, body.by)
    except ApprovalError as exc:
        raise _approval_error(exc) from exc
    return approval_out(proposal)


@router.post(
    "/approvals/{approval_id}/deny",
    response_model=m.ApprovalOut,
    responses=ERRORS,
    tags=["approvals"],
)
async def deny(ctx: Ctx, approval_id: str, body: m.DecisionRequest) -> m.ApprovalOut:
    try:
        proposal = await asyncio.to_thread(
            ctx.approvals.deny, approval_id, body.by, body.comment or ""
        )
    except ApprovalError as exc:
        raise _approval_error(exc) from exc
    return approval_out(proposal)


# --------------------------------------------------------------------------- scenarios


def _require_faults() -> None:
    if not faults_enabled():
        raise ApiError(
            403,
            "faults_disabled",
            "Fault injection is disabled. Start the API with AIOPS_ENABLE_FAULTS=1 (local "
            "demo clusters only).",
        )


@router.get("/scenarios", response_model=list[m.ScenarioOut], tags=["scenarios"])
async def scenarios(ctx: Ctx) -> list[m.ScenarioOut]:
    try:
        active = await asyncio.to_thread(ctx.faults.active)
    except Exception:
        active = None
    return [
        m.ScenarioOut(
            id=s.id,
            title=s.title,
            service=s.service or "",
            description=s.description.strip(),
            active=active is not None and active.upper() == s.id.upper(),
            injectable=injectable(s.id),
        )
        for s in ctx.scenarios.scenarios
    ]


@router.post(
    "/scenarios/revert",
    status_code=202,
    response_model=m.FaultResult,
    responses=ERRORS,
    tags=["scenarios"],
)
async def revert_scenarios(ctx: Ctx) -> m.FaultResult:
    """Restore the healthy baseline (runs in the background: rollouts take minutes)."""
    _require_faults()
    active = await asyncio.to_thread(ctx.faults.active)
    try:
        await asyncio.to_thread(ctx.faults.revert)
    except FaultError as exc:
        raise ApiError(409, "fault_error", str(exc)) from exc
    return m.FaultResult(
        scenario=active, status="reverting", message="Reverting to the healthy baseline."
    )


@router.post(
    "/scenarios/{scenario_id}/inject",
    response_model=m.FaultResult,
    responses=ERRORS,
    tags=["scenarios"],
)
async def inject_scenario(ctx: Ctx, scenario_id: str) -> m.FaultResult:
    """Inject a scenario's fault into the local cluster (one at a time, cluster lock)."""
    _require_faults()
    scenario = ctx.scenarios.get(scenario_id)
    if scenario is None:
        raise not_found("Scenario", scenario_id)
    if not injectable(scenario.id):
        raise ApiError(422, "not_injectable", f"{scenario.id} is the healthy baseline: no fault.")
    try:
        state = await asyncio.to_thread(ctx.faults.inject, scenario.id)
    except FaultError as exc:
        raise ApiError(409, "fault_error", str(exc)) from exc
    return m.FaultResult(
        scenario=state.scenario,
        status="injected",
        message=f"{scenario.id} injected at {state.injected_at}; revert with POST /api/scenarios/revert.",
    )
