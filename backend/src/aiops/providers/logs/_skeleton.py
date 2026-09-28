"""SKELETON, NOT REGISTERED: copy to ``providers/logs/<name>.py`` to add a logs provider.

Files starting with ``_`` are never imported by ``aiops.providers``, so this one is
inert. Steps (docs/portability.md, "How to add a provider"):

1. Copy this file to ``providers/logs/loki.py`` (for example) and rename the class.
2. Implement each method so the query returns the NEUTRAL columns listed in
   ``aiops.providers.logs`` (``window``, ``level``, ``count``, ``msg``, ...). If the
   vendor can't alias columns, map them in ``ToolRequest(columns=...)`` or override
   ``table()``.
3. Uncomment the ``PROVIDER_REGISTRY.register(...)`` line at the bottom.
4. Add the prompt fragment ``config/prompts/providers/logs/<name>/v1.md`` (tool names
   and query examples; it's rendered with the agent's variables, e.g. ``$index``).
5. Name the provider in the profile (``capabilities.logs.provider: <name>``) with its MCP
   server and ``tool_allowlist``; ``aiops profile validate`` then checks ``required``.
6. Add contract tests: golden queries per method + a replay of recorded tool results
   through the Log agent (tests/unit/test_providers.py shows the pattern).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from aiops.providers.base import ToolRequest
from aiops.providers.logs import LogScope, LogsProvider, LogWindow, iso


class SkeletonLogs(LogsProvider):
    name: ClassVar[str] = "skeleton"
    mcp: ClassVar[str] = "skeleton-mcp"
    required: ClassVar[tuple[str, ...]] = ()  # dotted settings keys, e.g. ("labels.service",)
    agent_tools: ClassVar[tuple[str, ...]] = ("query_logs",)
    note: ClassVar[str] = "example only"
    prompt_fragment: ClassVar[str | None] = "providers/logs/skeleton"
    default_fields: ClassVar[dict[str, str]] = {"timestamp": "timestamp", "level": "level"}

    def _request(self, query: str, window: LogWindow) -> ToolRequest:
        return ToolRequest(
            "query_logs", {"query": query, "start": iso(window.scope_start), "end": iso(window.end)}
        )

    def volume_by_level(self, scope: LogScope, window: LogWindow) -> ToolRequest:
        raise NotImplementedError("count lines BY window, level")

    def message_patterns(self, scope: LogScope, window: LogWindow, limit: int) -> ToolRequest:
        raise NotImplementedError("count, first_seen, last_seen BY window, level, msg")

    def versions_and_startups(self, scope: LogScope, window: LogWindow) -> ToolRequest:
        raise NotImplementedError("count, starts, first_seen BY window, version")

    def first_occurrences(
        self, scope: LogScope, window: LogWindow, phrase: str, limit: int
    ) -> ToolRequest:
        raise NotImplementedError("first `limit` lines since window.start starting with phrase")

    def ui_link(
        self,
        scope: LogScope,
        window: LogWindow,
        *,
        levels: Sequence[str] | None = None,
        phrase: str | None = None,
    ) -> str | None:
        return None  # e.g. a Grafana Explore link from settings.ui_link_template


# PROVIDER_REGISTRY.register(SkeletonLogs)  # from aiops.providers.registry import PROVIDER_REGISTRY
