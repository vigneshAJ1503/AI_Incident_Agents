"""``tickets/jira`` and ``tickets/mock``: JQL over the mcp-atlassian tool contract.

Both speak ``aiops.mcp.tickets`` (tool and field names of sooperset/mcp-atlassian);
``mock`` is our offline mock-tickets-mcp with the same contract.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, ClassVar

from aiops.mcp import tickets
from aiops.providers.base import ToolRequest
from aiops.providers.registry import PROVIDER_REGISTRY
from aiops.providers.tickets import Ticket, TicketsProvider


def jql_value(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def jql_list(values: Iterable[str]) -> str:
    return "(" + ", ".join(jql_value(v) for v in values) + ")"


def recency_clause(cutoff: datetime) -> str:
    """Open issues, or issues resolved since ``cutoff`` (absolute date: replayable)."""
    return f'(statusCategory != Done OR resolved >= "{cutoff:%Y-%m-%d}")'


def scope_jql(
    project: str, components: Sequence[str], labels: Sequence[str], cutoff: datetime
) -> str:
    parts = []
    if components:
        parts.append(f"component in {jql_list(components)}")
    if labels:
        parts.append(f"labels in {jql_list(labels)}")
    return (
        f"project = {jql_value(project)} AND ({' OR '.join(parts)}) AND {recency_clause(cutoff)} "
        "ORDER BY updated DESC"
    )


def keyword_jql(project: str, words: Iterable[str], cutoff: datetime) -> str:
    text = " OR ".join(f"text ~ {jql_value(w)}" for w in dict.fromkeys(words))
    return f"project = {jql_value(project)} AND ({text}) AND {recency_clause(cutoff)} ORDER BY updated DESC"


class JiraTickets(TicketsProvider):
    name: ClassVar[str] = "jira"
    mcp: ClassVar[str] = "sooperset/mcp-atlassian"
    required: ClassVar[tuple[str, ...]] = ("project_key",)
    agent_tools: ClassVar[tuple[str, ...]] = (tickets.SEARCH,)
    note: ClassVar[str] = "mcp-atlassian tool contract (Jira Cloud / Data Center)"
    prompt_fragment: ClassVar[str | None] = "providers/tickets/jira"
    search_tool: ClassVar[str] = tickets.SEARCH

    def _search(self, jql: str, limit: int) -> ToolRequest:
        return ToolRequest(
            tickets.SEARCH, {"jql": jql, "fields": tickets.TICKET_FIELDS, "limit": limit}
        )

    def scope_search(
        self,
        project: str,
        components: Sequence[str],
        labels: Sequence[str],
        cutoff: datetime,
        limit: int,
    ) -> ToolRequest:
        return self._search(scope_jql(project, components, labels, cutoff), limit)

    def keyword_search(
        self, project: str, words: Sequence[str], cutoff: datetime, limit: int
    ) -> ToolRequest:
        return self._search(keyword_jql(project, words, cutoff), limit)

    def tickets(self, data: Any, text: str = "") -> list[Ticket]:
        return tickets.parse_search(data, text)


class MockTickets(JiraTickets):
    name: ClassVar[str] = "mock"
    mcp: ClassVar[str] = "mcp-servers/mock-tickets-mcp"
    note: ClassVar[str] = "offline, same contract as jira"


PROVIDER_REGISTRY.register(JiraTickets)
PROVIDER_REGISTRY.register(MockTickets)
