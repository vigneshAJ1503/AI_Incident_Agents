"""Build guarded toolsets for capabilities from the environment config."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from aiops.core.config import Settings
from aiops.core.guardrails.audit import AuditSink, JsonlAuditSink
from aiops.mcp.breaker import BREAKERS, CircuitBreaker
from aiops.mcp.client import MCPClient, MCPClientError, ServerTarget
from aiops.mcp.fixtures import RecordingMCPClient, ReplayMCPClient
from aiops.mcp.toolset import Toolset
from aiops.observability.cache import ToolCallCache


class MCPRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        audit: AuditSink | None = None,
        overrides: Mapping[str, ServerTarget] | None = None,
        record_dir: Path | None = None,
        replay_dir: Path | None = None,
        replay_delay_s: float = 0.0,
        replay_lenient: bool = False,
    ) -> None:
        """
        ``overrides`` maps capability -> server target (e.g. an in-process test server).
        ``record_dir`` saves live MCP exchanges as fixtures; ``replay_dir`` serves them back.
        """
        if record_dir and replay_dir:
            raise ValueError("record_dir and replay_dir are mutually exclusive")
        self.settings = settings
        self.audit = audit or self._default_audit(settings)
        self._overrides = dict(overrides or {})
        self._record_dir = record_dir
        self._replay_dir = replay_dir
        self._replay_delay_s = replay_delay_s
        self._replay_lenient = replay_lenient
        #: The investigation's tool cache (PR-041): set by the orchestrator per investigation,
        #: used by agent (read-only) toolsets only, never by ``write_toolset``.
        self.tool_cache: ToolCallCache | None = None

    @classmethod
    def _default_audit(cls, settings: Settings) -> AuditSink:
        jsonl = JsonlAuditSink(cls._audit_path(settings))
        if settings.storage.audit != "postgres":
            return jsonl
        from aiops.store.db import database_url
        from aiops.store.sync_stores import SqlAuditSink

        return SqlAuditSink(database_url(settings), settings.storage.db_schema, fallback=jsonl)

    @staticmethod
    def _audit_path(settings: Settings) -> Path:
        path = Path(settings.guardrails.audit_log_path)
        return path if path.is_absolute() else settings.config_dir.parent / path

    def write_tools(self) -> list[str]:
        """Every write tool of the profile (data that names one is flagged, PR-042)."""
        return sorted({t for c in self.settings.capabilities.values() for t in c.write_allowlist})

    def client(self, capability: str) -> MCPClient:
        config = self.settings.capability(capability)
        if self._replay_dir is not None:
            return ReplayMCPClient(
                capability,
                self._replay_dir / f"{capability}.json",
                lenient=self._replay_lenient,
                delay_s=self._replay_delay_s,
            )
        client = (
            MCPClient(capability, self._overrides[capability], timeout_s=config.mcp.timeout_s)
            if capability in self._overrides
            else MCPClient.from_config(capability, config.mcp)
        )
        if self._record_dir is not None:
            return RecordingMCPClient(client, self._record_dir / f"{capability}.json")
        return client

    def breaker(self, capability: str) -> CircuitBreaker | None:
        """The capability's process-wide circuit breaker (none for replays: recorded data
        can't be "down")."""
        if self._replay_dir is not None:
            return None
        config = self.settings.capability(capability)
        target = self._overrides.get(capability)
        if target is None:
            key = config.mcp.url or " ".join([config.mcp.command or "", *config.mcp.args])
        else:  # in-process test servers: one breaker per server object
            key = target if isinstance(target, str) else f"in-process:{id(target)}"
        return BREAKERS.get(capability, key, config.limits)

    @asynccontextmanager
    async def _connected(self, capability: str) -> AsyncIterator[MCPClient]:
        """A connected client, guarded by the circuit breaker (PR-042): an open circuit
        fails at once; connect errors count as failures, a connect as a success."""
        breaker = self.breaker(capability)
        if breaker is not None:
            breaker.check()
        client = self.client(capability)
        try:
            await client.connect()
        except MCPClientError as exc:
            if breaker is not None:
                breaker.record_failure(str(exc))
            raise
        if breaker is not None:
            breaker.record_success()
        try:
            yield client
        finally:
            await client.close()

    @asynccontextmanager
    async def write_toolset(
        self, capability: str, *, actor: str, investigation_id: str | None = None
    ) -> AsyncIterator[Toolset]:
        """Toolset over ``write_allowlist`` ONLY (no read tools). Used exclusively by the
        approval executor for APPROVED proposals; agents never get one."""
        config = self.settings.capability(capability)
        async with self._connected(capability) as client:
            yield Toolset(
                capability,
                config,
                client,
                agent=actor,
                guardrails=self.settings.guardrails,
                audit=self.audit,
                investigation_id=investigation_id,
                allowlist=config.write_allowlist,
            )

    @asynccontextmanager
    async def toolset(
        self, capability: str, *, agent: str, investigation_id: str | None = None
    ) -> AsyncIterator[Toolset]:
        config = self.settings.capability(capability)
        async with self._connected(capability) as client:
            yield Toolset(
                capability,
                config,
                client,
                agent=agent,
                guardrails=self.settings.guardrails,
                audit=self.audit,
                investigation_id=investigation_id,
                watch_tools=self.write_tools(),
                breaker=self.breaker(capability),
                cache=self.tool_cache,
            )
