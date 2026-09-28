"""Background investigations for the API.

``POST /investigations`` returns 202 at once; the orchestrator runs as an asyncio task in
the API process (at most ``api.max_running_investigations`` at a time). Its events go to
the shared ``EventBus`` (live SSE subscribers) and, batched every few hundred ms, to the
evidence store together with snapshots of the investigation, so ``GET`` and SSE work for
another process or after a restart too. The final investigation is saved when it ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from aiops.api.errors import ApiError
from aiops.core.config import Settings
from aiops.core.events import EventBus, InvestigationEvent
from aiops.core.models import (
    Incident,
    Investigation,
    InvestigationStatus,
    new_id,
    utcnow,
)
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.replay import load_replay
from aiops.store.repository import InvestigationStore

log = logging.getLogger("aiops.api.runner")

RunMode = Literal["live", "replay"]
#: ``(settings, bus, scenario or None)`` -> an orchestrator (replay when a scenario is given).
OrchestratorFactory = Callable[[Settings, EventBus, str | None], Orchestrator]
#: Events after which the stored investigation document is refreshed.
SNAPSHOT_EVENTS = {
    "plan_created",
    "clarification_needed",
    "agent_finished",
    "hypothesis_ranked",
    "report_ready",
}
TERMINAL = {
    InvestigationStatus.COMPLETED,
    InvestigationStatus.PARTIAL,
    InvestigationStatus.FAILED,
    InvestigationStatus.CANCELLED,
}


#: Seconds each recorded tool call of a replay waits, so the Web UI's live view unfolds at a
#: watchable pace (a replay otherwise finishes in milliseconds). 0 = instant (default).
REPLAY_DELAY_ENV = "AIOPS_REPLAY_TOOL_DELAY_S"


def replay_tool_delay_s() -> float:
    raw = os.environ.get(REPLAY_DELAY_ENV, "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        log.warning("ignoring %s=%r: not a number of seconds", REPLAY_DELAY_ENV, raw)
        return 0.0
    return min(max(value, 0.0), 10.0)


def default_orchestrator(settings: Settings, bus: EventBus, scenario: str | None) -> Orchestrator:
    replay = load_replay(settings, scenario) if scenario else None
    if replay is not None:
        replay.tool_delay_s = replay_tool_delay_s()
    return Orchestrator(settings, replay=replay, bus=bus)


@dataclass
class RunHandle:
    id: str
    mode: RunMode
    scenario: str | None
    orchestrator: Orchestrator
    placeholder: Investigation
    created_at: datetime
    task: asyncio.Task[None] | None = None
    result: Investigation | None = None
    pending: list[InvestigationEvent] = field(default_factory=list)
    unsubscribe: Callable[[], None] | None = None

    def snapshot(self) -> Investigation:
        """The freshest state: final result, else the orchestrator's live state."""
        if self.result is not None:
            return self.result
        live = self.orchestrator.running(self.id)
        return live if live is not None else self.placeholder


class InvestigationRunner:
    def __init__(
        self,
        settings: Settings,
        store: InvestigationStore,
        bus: EventBus,
        *,
        orchestrator_factory: OrchestratorFactory = default_orchestrator,
        max_running: int | None = None,
        flush_interval_s: float = 0.25,
        retain_s: float = 300.0,
    ) -> None:
        self.settings = settings
        self.store = store
        self.bus = bus
        self.factory = orchestrator_factory
        self.max_running = max_running or settings.api.max_running_investigations
        self.flush_interval_s = flush_interval_s
        self.retain_s = retain_s
        self._active: dict[str, RunHandle] = {}

    # -- queries -----------------------------------------------------------------------------

    def is_live(self, investigation_id: str) -> bool:
        return investigation_id in self._active

    def handle(self, investigation_id: str) -> RunHandle | None:
        return self._active.get(investigation_id)

    def snapshot(self, investigation_id: str) -> Investigation | None:
        handle = self._active.get(investigation_id)
        return handle.snapshot().model_copy(deep=True) if handle else None

    @property
    def running(self) -> int:
        return len(self._active)

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(
        self,
        request: InvestigationRequest,
        *,
        scenario: str | None = None,
        investigation_id: str | None = None,
        created_at: datetime | None = None,
        history: list[InvestigationEvent] | None = None,
    ) -> RunHandle:
        """Save a pending investigation and run it in the background."""
        if len(self._active) >= self.max_running:
            raise ApiError(
                429,
                "too_many_investigations",
                f"{len(self._active)} investigation(s) already running in this API process "
                f"(api.max_running_investigations = {self.max_running}); try again shortly.",
            )
        inv_id = investigation_id or new_id("inv")
        if inv_id in self._active:
            raise ApiError(409, "already_running", f"Investigation '{inv_id}' is already running")
        mode: RunMode = "replay" if scenario else "live"
        orchestrator = self.factory(self.settings, self.bus, scenario)
        placeholder = Investigation(
            id=inv_id,
            incident=Incident(
                title=request.question.strip()[:200] or "Incident",
                service=request.service,
                environment=request.environment,
            ),
            status=InvestigationStatus.PENDING,
            mode=mode,
            created_at=created_at or utcnow(),
        )
        if history:
            self.bus.restore(inv_id, history)  # continue the seq numbering (clarify)
        handle = RunHandle(
            id=inv_id,
            mode=mode,
            scenario=scenario,
            orchestrator=orchestrator,
            placeholder=placeholder,
            created_at=placeholder.created_at,
        )
        await self.store.save(placeholder)
        handle.unsubscribe = self.bus.subscribe(handle.pending.append, inv_id)
        self._active[inv_id] = handle
        request.mode = mode
        handle.task = asyncio.create_task(
            self._run(handle, request), name=f"investigation-{inv_id}"
        )
        return handle

    def cancel(self, investigation_id: str) -> bool:
        handle = self._active.get(investigation_id)
        if handle is None or handle.result is not None:
            return False
        if not handle.orchestrator.cancel(investigation_id) and handle.task is not None:
            handle.task.cancel()  # not started yet: stop the task itself
        return True

    async def shutdown(self) -> None:
        """Cancel what still runs (API shutdown); each run saves its final state."""
        tasks = [h.task for h in self._active.values() if h.task is not None]
        for inv_id in list(self._active):
            self.cancel(inv_id)
        if tasks:
            await asyncio.wait(tasks, timeout=10)

    # -- internals ---------------------------------------------------------------------------

    async def _run(self, handle: RunHandle, request: InvestigationRequest) -> None:
        stop = asyncio.Event()
        flusher = asyncio.create_task(self._flush_loop(handle, stop))
        try:
            try:
                inv = await handle.orchestrator.investigate(request, investigation_id=handle.id)
            except asyncio.CancelledError:  # cancelled before the orchestrator took over
                inv = handle.snapshot().model_copy(
                    update={"status": InvestigationStatus.CANCELLED, "completed_at": utcnow()}
                )
                self.bus.publish(
                    "error", handle.id, message="Investigation cancelled", recoverable=False
                )
                self.bus.publish(
                    "investigation_finished", handle.id, status="cancelled", duration_ms=0
                )
            except Exception as exc:  # the orchestrator never raises; belt and braces
                log.exception("investigation %s crashed", handle.id)
                inv = handle.snapshot().model_copy(
                    update={"status": InvestigationStatus.FAILED, "completed_at": utcnow()}
                )
                self.bus.publish("error", handle.id, message=str(exc)[:300], recoverable=False)
                self.bus.publish(
                    "investigation_finished", handle.id, status="failed", duration_ms=0
                )
            # A resumed (clarified) investigation keeps its original creation time.
            handle.result = inv.model_copy(update={"created_at": handle.created_at})
        finally:
            stop.set()
            await asyncio.wait([flusher])
            await self._finish(handle)

    async def _finish(self, handle: RunHandle) -> None:
        try:
            await self._flush(handle, snapshot=False)
            if handle.result is not None:
                await self.store.save(handle.result)
        except Exception:
            log.exception("could not save investigation %s", handle.id)
        finally:
            if handle.unsubscribe:
                handle.unsubscribe()
            self._active.pop(handle.id, None)
            loop = asyncio.get_running_loop()
            loop.call_later(self.retain_s, self._forget, handle.id)

    def _forget(self, investigation_id: str) -> None:
        if investigation_id not in self._active:
            self.bus.forget(investigation_id)

    async def _flush_loop(self, handle: RunHandle, stop: asyncio.Event) -> None:
        """Flush every ``flush_interval_s`` until ``stop`` (never cancelled mid-write)."""
        while not stop.is_set():
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(self.flush_interval_s):
                    await stop.wait()
            if stop.is_set():
                return
            try:
                await self._flush(handle, snapshot=True)
            except Exception as exc:  # the store may blip; the events stay queued
                log.warning("flush of %s failed: %s", handle.id, exc)

    async def _flush(self, handle: RunHandle, *, snapshot: bool) -> None:
        batch = list(handle.pending)
        if not batch:
            return
        await self.store.save_events(batch)
        del handle.pending[: len(batch)]  # only once stored (a failed flush is retried)
        if snapshot and any(e.type in SNAPSHOT_EVENTS for e in batch):
            live = handle.orchestrator.running(handle.id)
            if live is not None:
                await self.store.save(
                    live.model_copy(deep=True, update={"created_at": handle.created_at})
                )
