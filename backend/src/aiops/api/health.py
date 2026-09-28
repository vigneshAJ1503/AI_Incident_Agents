"""Cheap, cached health checks for ``GET /health`` and the live/replay decision.

``aiops doctor`` does the thorough job (MCP handshake, contract, smoke queries: seconds).
The API only needs "is the capability's server there at all?", so it opens a TCP
connection to each HTTP MCP server (stdio servers: the command is on PATH), in parallel,
with a short timeout, and caches the answer. Secrets are never part of the output.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from aiops.core.config import CapabilityConfig, Settings
from aiops.llm.factory import config_problems
from aiops.llm.factory import llm_configured as factory_llm_configured

Probe = Callable[[CapabilityConfig, float], Awaitable[bool]]


async def tcp_probe(cap: CapabilityConfig, timeout: float) -> bool:
    """True when the capability's MCP server accepts a connection (or its command exists)."""
    mcp = cap.mcp
    if mcp.transport == "stdio":
        return bool(mcp.command and shutil.which(mcp.command))
    parts = urlsplit(mcp.url or "")
    if not parts.hostname:
        return False
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        async with asyncio.timeout(timeout):
            _, writer = await asyncio.open_connection(parts.hostname, port)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()
    return True


def llm_configured(settings: Settings) -> bool:
    """A real, hosted LLM is usable: every field its provider needs is set (openai_compat,
    anthropic, azure_openai: endpoint/key/api-version; bedrock: an AWS region) plus an
    agent model. No network call, and the key is never part of the answer."""
    return factory_llm_configured(settings.llm)


def llm_missing(settings: Settings) -> list[str]:
    """Why ``llm_configured`` is false, e.g. ``["llm.api_key is not set (ANTHROPIC_API_KEY)"]``."""
    if settings.llm.provider == "fake":
        return ["llm.provider is 'fake' (set LLM_PROVIDER or the profile's llm block)"]
    return config_problems(settings.llm)


class CapabilityHealth:
    def __init__(
        self,
        settings: Settings,
        *,
        probe: Probe = tcp_probe,
        ttl_s: float | None = None,
        timeout_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self._probe = probe
        self._ttl = settings.api.health_cache_s if ttl_s is None else ttl_s
        self._timeout = settings.api.health_timeout_s if timeout_s is None else timeout_s
        self._clock = clock
        self._cached: dict[str, str] | None = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def status(self) -> dict[str, str]:
        """``{capability: ok|down|disabled}`` for every configured capability."""
        async with self._lock:
            if self._cached is not None and self._clock() - self._at < self._ttl:
                return dict(self._cached)
            caps = sorted(self.settings.capabilities.items())
            enabled = [(name, cap) for name, cap in caps if cap.enabled]
            results = await asyncio.gather(*(self._probe(cap, self._timeout) for _, cap in enabled))
            status = {name: "disabled" for name, cap in caps if not cap.enabled}
            status.update(
                {
                    name: "ok" if ok else "down"
                    for (name, _), ok in zip(enabled, results, strict=True)
                }
            )
            self._cached, self._at = status, self._clock()
            return dict(status)

    async def all_reachable(self) -> bool:
        status = await self.status()
        return bool(status) and all(v in ("ok", "disabled") for v in status.values())
