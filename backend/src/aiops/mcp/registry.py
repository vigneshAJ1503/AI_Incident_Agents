"""Build guarded toolsets for capabilities from the environment config."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from aiops.core.config import Settings
from aiops.core.guardrails.audit import AuditSink, JsonlAuditSink
from aiops.mcp.client import MCPClient, ServerTarget
from aiops.mcp.toolset import Toolset


class MCPRegistry:
    def __init__(
        self,
        settings: Settings,
        *,
        audit: AuditSink | None = None,
        overrides: Mapping[str, ServerTarget] | None = None,
    ) -> None:
        """``overrides`` maps capability -> server target (e.g. an in-process test server)."""
        self.settings = settings
        self.audit = audit or JsonlAuditSink(self._audit_path(settings))
        self._overrides = dict(overrides or {})

    @staticmethod
    def _audit_path(settings: Settings) -> Path:
        path = Path(settings.guardrails.audit_log_path)
        return path if path.is_absolute() else settings.config_dir.parent / path

    def client(self, capability: str) -> MCPClient:
        config = self.settings.capability(capability)
        if capability in self._overrides:
            return MCPClient(
                capability, self._overrides[capability], timeout_s=config.mcp.timeout_s
            )
        return MCPClient.from_config(capability, config.mcp)

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
