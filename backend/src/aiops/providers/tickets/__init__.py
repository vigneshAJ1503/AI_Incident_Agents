"""The ``tickets`` capability: what the Tickets agent asks, and the neutral ``Ticket``.

Providers turn "open or recently resolved tickets for these components/labels" and "...
mentioning these words" into the tracker's query language and tools, and parse results
into ``aiops.mcp.tickets.Ticket`` (the neutral ticket model).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from aiops.mcp.tickets import Ticket
from aiops.providers.base import Provider, ToolRequest

__all__ = ["Ticket", "TicketsProvider"]


class TicketsProvider(Provider):
    """Interface of a ``tickets`` provider (override every ``NotImplementedError``)."""

    capability: ClassVar[str] = "tickets"
    #: The search tool (evidence source of per-ticket evidence: ``tickets.<tool>``).
    search_tool: ClassVar[str]

    def scope_search(
        self,
        project: str,
        components: Sequence[str],
        labels: Sequence[str],
        cutoff: datetime,
        limit: int,
    ) -> ToolRequest:
        """Tickets in ``project`` with any of the components OR labels that are open or
        were resolved since ``cutoff``, most recently updated first."""
        raise NotImplementedError

    def keyword_search(
        self, project: str, words: Sequence[str], cutoff: datetime, limit: int
    ) -> ToolRequest:
        """Tickets in ``project`` whose text mentions any word (``word*`` = prefix), open or
        resolved since ``cutoff``, most recently updated first."""
        raise NotImplementedError

    def tickets(self, data: Any, text: str = "") -> list[Ticket]:
        """Normalize a search result into neutral tickets."""
        raise NotImplementedError
