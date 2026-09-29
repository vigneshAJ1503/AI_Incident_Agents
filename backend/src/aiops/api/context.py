"""Everything a request handler needs, built once per API process from the profile."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from aiops.api.health import CapabilityHealth, llm_configured
from aiops.api.runner import InvestigationRunner, OrchestratorFactory, default_orchestrator
from aiops.api.scenarios import FaultController, KubectlFaultController, ScenarioCatalog
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import ConfigError, Settings, load_settings
from aiops.core.events import EventBus
from aiops.core.guardrails.approvals import ApprovalExecutor, ApprovalService
from aiops.llm.factory import create_provider
from aiops.mcp.registry import MCPRegistry
from aiops.observability.llm import instrument
from aiops.orchestrator.intent import IntentClassifier
from aiops.store.db import StoreError
from aiops.store.repository import InvestigationStore

log = logging.getLogger("aiops.api")

ExecutorFactory = Callable[[ApprovalService], ApprovalExecutor]


def default_executor_factory(settings: Settings) -> ExecutorFactory:
    """Approvals execute through the existing ``ApprovalExecutor`` (the only write path)."""

    def build(service: ApprovalService) -> ApprovalExecutor:
        return ApprovalExecutor(service, MCPRegistry(settings))

    return build


@dataclass
class ApiContext:
    settings: Settings
    store: InvestigationStore
    approvals: ApprovalService
    catalog: ServiceCatalog
    scenarios: ScenarioCatalog
    bus: EventBus
    runner: InvestigationRunner
    health: CapabilityHealth
    faults: FaultController
    executor_factory: ExecutorFactory
    store_ok: bool = True
    background: set[asyncio.Task[None]] = field(default_factory=set)
    #: Start the stuck-investigation reaper on startup (tests start it by hand).
    reaper: bool = True
    #: The chat box's intent classifier (PR-041); built on first use.
    intent: IntentClassifier | None = None

    def classifier(self) -> IntentClassifier:
        """Rules + the ``fast`` LLM role as a fallback when a hosted LLM is configured."""
        if self.intent is None:
            llm = None
            if llm_configured(self.settings):
                try:
                    llm = instrument(create_provider(self.settings.llm), self.settings)
                except ConfigError as exc:  # configured but unusable: rules only
                    log.warning("intent classifier without LLM: %s", exc)
            caps = [(n, c.provider) for n, c in self.settings.capabilities.items()]
            self.intent = IntentClassifier.for_profile(self.catalog, caps, llm=llm)
        return self.intent

    async def startup(self) -> None:
        """Create/upgrade the store's tables. An unreachable store doesn't stop the API:
        ``/health`` says so and store-backed routes answer 503 until it is back."""
        try:
            await asyncio.to_thread(self.store.migrate)
            self.store_ok = True
        except StoreError as exc:
            log.error("evidence store unavailable: %s", exc)
            self.store_ok = False
        if self.reaper and self.settings.api.stuck_after_s > 0:
            task = asyncio.create_task(self._reap_forever(), name="stuck-investigation-reaper")
            self.background.add(task)
            task.add_done_callback(self.background.discard)

    async def _reap_forever(self) -> None:
        """Reap once at startup (orphans of a crashed process), then every interval."""
        while True:
            try:
                await self.runner.reap_stuck()
            except Exception as exc:  # the store may be down; try again next time
                log.warning("stuck-investigation reaper failed: %s", exc)
            await asyncio.sleep(self.settings.api.reaper_interval_s)

    async def shutdown(self) -> None:
        for task in list(self.background):
            task.cancel()
        if self.background:
            await asyncio.wait(list(self.background), timeout=5)
        await self.runner.shutdown()
        await self.store.close()


def build_context(
    settings: Settings | None = None,
    *,
    store: InvestigationStore | None = None,
    approvals: ApprovalService | None = None,
    orchestrator_factory: OrchestratorFactory = default_orchestrator,
    faults: FaultController | None = None,
    health: CapabilityHealth | None = None,
    executor_factory: ExecutorFactory | None = None,
    reaper: bool = True,
) -> ApiContext:
    """The production wiring (``AIOPS_PROFILE``); tests override pieces."""
    settings = settings or load_settings()
    store = store or InvestigationStore.from_settings(settings)
    bus = EventBus()
    return ApiContext(
        settings=settings,
        store=store,
        approvals=approvals or ApprovalService.from_settings(settings),
        catalog=ServiceCatalog.from_settings(settings),
        scenarios=ScenarioCatalog.load(settings.config_dir),
        bus=bus,
        runner=InvestigationRunner(settings, store, bus, orchestrator_factory=orchestrator_factory),
        health=health or CapabilityHealth(settings),
        faults=faults or KubectlFaultController(settings.config_dir.parent),
        executor_factory=executor_factory or default_executor_factory(settings),
        reaper=reaper,
    )
