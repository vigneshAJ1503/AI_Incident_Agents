"""``knowledge/postgres_fts``: markdown runbooks in Postgres full-text search (ADR-0004)."""

from __future__ import annotations

from typing import ClassVar

from aiops.providers.base import Provider
from aiops.providers.registry import PROVIDER_REGISTRY


class PostgresFtsKnowledge(Provider):
    capability: ClassVar[str] = "knowledge"
    name: ClassVar[str] = "postgres_fts"
    mcp: ClassVar[str] = "mcp-servers/knowledge-mcp"
    agent_tools: ClassVar[tuple[str, ...]] = ("search", "get_doc", "list_docs")
    note: ClassVar[str] = "markdown runbooks -> Postgres full-text search"


PROVIDER_REGISTRY.register(PostgresFtsKnowledge)
