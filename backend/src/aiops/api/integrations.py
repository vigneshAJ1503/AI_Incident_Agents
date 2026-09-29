"""Settings -> Integrations (PR-046): configure capabilities from the Web UI.

    GET  /api/integrations                    every capability: provider, status, fields, secrets
    GET  /api/integrations/audit              who changed which field (never a value)
    GET  /api/integrations/{capability}       one of them
    PUT  /api/integrations/{capability}       save overrides (validated, audited, applied)
    POST /api/integrations/{capability}/test  ``aiops doctor`` for that capability (+ a draft)

Overrides live in the evidence store as an overlay on the active profile's YAML (which
stays the source of defaults, ``core/integrations.py``). Saving validates the result with
the profile's own Pydantic models and ``profile validate`` checks, stores it with an audit
row, and swaps the API's settings for **new** investigations; running investigations keep
the settings they started with. Secrets are write-only and encrypted with
``AIOPS_SECRETS_KEY``. With ``api.auth`` on, saving and testing need an authenticated caller.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from aiops.api import models as m
from aiops.api.auth import principal_of
from aiops.api.context import ApiContext
from aiops.api.errors import ApiError, not_found
from aiops.api.integration_service import IntegrationService
from aiops.api.routes import rate_limited
from aiops.core.doctor import DoctorReport, Status, check_capability
from aiops.core.integrations import (
    CAPABILITIES,
    FieldChange,
    IntegrationError,
    IntegrationUpdate,
    describe,
)

log = logging.getLogger("aiops.api.integrations")
router = APIRouter(prefix="/api/integrations", tags=["integrations"])

#: Per connect and per smoke call of "Test connection" (the doctor default is 10 s).
TEST_TIMEOUT_S = 8.0


# --------------------------------------------------------------------------- helpers


def get_ctx(request: Request) -> ApiContext:
    ctx: ApiContext = request.app.state.ctx
    return ctx


Ctx = Annotated[ApiContext, Depends(get_ctx)]


def service(ctx: ApiContext) -> IntegrationService:
    if ctx.integrations is None or not ctx.store_ok:
        raise ApiError(
            503,
            "store_unavailable",
            "Integration settings live in the evidence store (Postgres), which is unavailable.",
        )
    return ctx.integrations


def known(ctx: ApiContext, capability: str) -> str:
    if capability not in CAPABILITIES and capability not in ctx.settings.capabilities:
        raise not_found("Capability", capability)
    return capability


def require_writer(request: Request) -> str:
    """Saving and testing need an authenticated caller when ``api.auth`` is on (reads don't
    need more than the API already asks). Returns the actor recorded in the audit."""
    ctx = get_ctx(request)
    principal = principal_of(request)
    if ctx.settings.api.auth_mode != "none" and not principal.authenticated:
        raise ApiError(
            401,
            "unauthorized",
            "Changing or testing integrations needs an authenticated caller (X-API-Key).",
        )
    return principal.subject


def to_update(body: m.IntegrationUpdateRequest | None) -> IntegrationUpdate:
    if body is None:
        return IntegrationUpdate()
    return IntegrationUpdate(
        enabled=body.enabled,
        provider=body.provider or None,
        fields=dict(body.fields),
        secrets=dict(body.secrets),
        reset=body.reset,
    )


def integration_error(exc: IntegrationError) -> ApiError:
    return ApiError(exc.status, exc.code, str(exc))


async def integration_out(ctx: ApiContext, capability: str) -> m.IntegrationOut:
    svc = service(ctx)
    status = (await ctx.health.status()).get(capability, "not_configured")
    return m.IntegrationOut.model_validate(
        describe(
            capability,
            svc.base,
            ctx.settings,
            svc.overrides.get(capability),
            status,
            svc.box,
            svc.notes.get(capability),
        )
    )


def change_out(changes: list[FieldChange] | tuple[FieldChange, ...]) -> list[m.FieldChangeOut]:
    return [m.FieldChangeOut(field=c.field, change=c.change) for c in changes]


_STATUS = {Status.OK: "ok", Status.WARN: "warn", Status.FAIL: "fail", Status.SKIP: "skip"}


def test_result(capability: str, report: DoctorReport, started: float) -> m.IntegrationTestResult:
    checks = [
        m.IntegrationCheck(
            check=c.check,
            status=_STATUS[c.status],  # type: ignore[arg-type]
            detail=c.detail,
            hint=c.hint,
            latency_ms=c.latency_ms,
        )
        for c in report.checks
    ]
    statuses = {c.status for c in checks}
    if not checks or statuses == {"skip"}:
        overall = "skip"
    else:
        overall = next((s for s in ("fail", "warn") if s in statuses), "ok")
    return m.IntegrationTestResult(
        capability=capability,
        status=overall,  # type: ignore[arg-type]
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
        checks=checks,
    )


# --------------------------------------------------------------------------- routes


@router.get("", response_model=m.IntegrationList)
async def list_integrations(ctx: Ctx) -> m.IntegrationList:
    """Every capability an agent binds to (configured or not) and any extra ones of the
    profile. Secret values are never returned."""
    svc = service(ctx)
    names = list(CAPABILITIES) + sorted(set(ctx.settings.capabilities) - set(CAPABILITIES))
    return m.IntegrationList(
        profile=svc.profile,
        secrets_enabled=svc.box is not None,
        items=[await integration_out(ctx, name) for name in names],
    )


@router.get("/audit", response_model=list[m.IntegrationAuditOut])
async def integration_audit(
    ctx: Ctx,
    capability: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[m.IntegrationAuditOut]:
    """Newest first: who changed which fields of which capability (never a value)."""
    entries = await service(ctx).audit(capability, limit)
    return [
        m.IntegrationAuditOut(
            id=e.id,
            recorded_at=e.recorded_at,
            capability=e.capability,
            actor=e.actor,
            action=e.action,
            changes=change_out(e.changes),
        )
        for e in entries
    ]


@router.get("/{capability}", response_model=m.IntegrationOut)
async def get_integration(ctx: Ctx, capability: str) -> m.IntegrationOut:
    return await integration_out(ctx, known(ctx, capability))


@router.put(
    "/{capability}",
    response_model=m.IntegrationSaved,
    dependencies=[rate_limited("integrations")],
)
async def save_integration(
    ctx: Ctx,
    capability: str,
    body: m.IntegrationUpdateRequest,
    actor: Annotated[str, Depends(require_writer)],
) -> m.IntegrationSaved:
    """Validate and save overrides of ``capability``; ``null`` reverts a field (clears a
    secret), ``reset: true`` drops every override first. Applies to new investigations
    at once (no restart). Every change is audited (field names, never values)."""
    svc = service(ctx)
    known(ctx, capability)
    try:
        settings, changes, warnings = await svc.save(capability, to_update(body), actor)
    except IntegrationError as exc:
        raise integration_error(exc) from exc
    if changes:
        ctx.apply_settings(settings)
    return m.IntegrationSaved(
        integration=await integration_out(ctx, capability),
        changes=change_out(changes),
        warnings=warnings,
    )


@router.post(
    "/{capability}/test",
    response_model=m.IntegrationTestResult,
    dependencies=[rate_limited("integrations")],
)
async def test_integration(
    ctx: Ctx,
    capability: str,
    actor: Annotated[str, Depends(require_writer)],
    body: m.IntegrationUpdateRequest | None = None,
) -> m.IntegrationTestResult:
    """The ``aiops doctor`` checks of one capability: config, reachability, tool contract,
    a read-only smoke call and a few catalog identifiers. With a body, they run on the
    saved settings plus that unsaved draft (nothing is stored)."""
    svc = service(ctx)
    known(ctx, capability)
    started = time.perf_counter()
    try:
        draft = to_update(body)
        settings = svc.draft(capability, draft) if body is not None else ctx.settings
    except IntegrationError as exc:
        raise integration_error(exc) from exc
    cap = settings.capabilities.get(capability)
    if cap is None or not cap.enabled:
        why = "not configured in the profile" if cap is None else "disabled"
        return m.IntegrationTestResult(
            capability=capability,
            status="skip",
            duration_ms=0.0,
            checks=[
                m.IntegrationCheck(
                    check="config",
                    status="skip",
                    detail=f"{capability} is {why}",
                    hint="enable it to test the connection",
                    latency_ms=None,
                )
            ],
        )
    log.info("integration %s: test connection by %s", capability, actor)
    report = await check_capability(
        settings, capability, timeout_s=TEST_TIMEOUT_S, overrides=svc.mcp_overrides
    )
    return test_result(capability, report, started)
