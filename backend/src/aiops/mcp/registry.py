"""Build guarded toolsets for capabilities from the environment config."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from aiops.core.config import Settings
from aiops.core.guardrails.audit import AuditSink, JsonlAuditSink
from aiops.mcp.client import MCPClient, ServerTarget
from aiops.mcp.fixtures import RecordingMCPClient, ReplayMCPClient
from aiops.mcp.toolset import Toolset


class MCPRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        audit: AuditSink | None = None,
        overrides: Mapping[str, ServerTarget] | None = None,
        record_dir: Path | None = None,
        replay_dir: Path | None = None,
    ) -> None:
        """
        ``overrides`` maps capability -> server target (e.g. an in-process test server).
        ``record_dir`` saves live MCP exchanges as fixtures; ``replay_dir`` serves them back.
        """
        if record_dir and replay_dir:
            raise ValueError("record_dir and replay_dir are mutually exclusive")
        self.settings = settings
        self.audit = audit or JsonlAuditSink(self._audit_path(settings))
        self._overrides = dict(overrides or {})
        self._record_dir = record_dir
        self._replay_dir = replay_dir

    @staticmethod
    def _audit_path(settings: Settings) -> Path:
        path = Path(settings.guardrails.audit_log_path)
        return path if path.is_absolute() else settings.config_dir.parent / path

    def client(self, capability: str) -> MCPClient:
        config = self.settings.capability(capability)
        if self._replay_dir is not None:
            return ReplayMCPClient(capability, self._replay_dir / f"{capability}.json")
        client = (
            MCPClient(capability, self._overrides[capability], timeout_s=config.mcp.timeout_s)
            if capability in self._overrides
            else MCPClient.from_config(capability, config.mcp)
        )
        if self._record_dir is not None:
            return RecordingMCPClient(client, self._record_dir / f"{capability}.json")
        return client

    @asynccontextmanager
    async def write_toolset(
        self, capability: str, *, actor: str, investigation_id: str | None = None
    ) -> AsyncIterator[Toolset]:
        """Toolset over ``write_allowlist`` ONLY (no read tools). Used exclusively by the
        approval executor for APPROVED proposals; agents never get one."""
        config = self.settings.capability(capability)
        async with self.client(capability) as client:
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
        async with self.client(capability) as client:
            yield Toolset(
                capability,
                config,
                client,
                agent=agent,
                guardrails=self.settings.guardrails,
                audit=self.audit,
                investigation_id=investigation_id,
            )
