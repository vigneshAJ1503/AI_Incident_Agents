"""Contract test: both ``tickets`` providers answer the same calls with the shapes we rely on.

    make infra-up mock-tickets-up seed-tickets && make test-integration

* mock: mock-tickets-mcp on :8109 (always).
* jira: mcp-atlassian on :8102 against a Jira Cloud site; skipped unless JIRA_URL is set and
  the `jira` compose profile is up (docs/setup/jira.md). Needs at least one issue in OPS.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from aiops.mcp import tickets
from aiops.mcp.client import MCPClient, MCPClientError

pytestmark = pytest.mark.integration

PROVIDERS = {
    "mock": os.environ.get("MOCK_TICKETS_MCP_URL", "http://localhost:8109/mcp"),
    "jira": os.environ.get("JIRA_MCP_URL", "http://localhost:8102/mcp"),
}
PROJECT = os.environ.get("TICKETS_PROJECT_KEY", "OPS")


def target(provider: str) -> str:
    if provider == "jira" and not os.environ.get("JIRA_URL"):
        pytest.skip("JIRA_URL not set: no Jira Cloud site configured")
    return PROVIDERS[provider]


async def connect(provider: str) -> MCPClient:
    client = MCPClient(provider, target(provider), timeout_s=30, connect_attempts=1)
    try:
        await client.connect()
    except MCPClientError as exc:
        pytest.fail(f"{provider} tickets MCP is not reachable: {exc}")
    return client


def payload(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.text
    return tickets.as_payload(result.structured, result.text)


@pytest.mark.parametrize("provider", ["mock", "jira"])
async def test_tickets_contract(provider: str) -> None:
    client = await connect(provider)
    try:
        names = {t.name for t in await client.list_tools()}
        assert set(tickets.READ_TOOLS) <= names
        schemas = {t.name: t.input_schema for t in await client.list_tools()}
        assert {"jql", "fields", "limit"} <= set(schemas[tickets.SEARCH]["properties"])
        assert {"issue_key", "fields"} <= set(schemas[tickets.GET_ISSUE]["properties"])

        # The exact search shape the tickets agent issues.
        result = await client.call_tool(
            tickets.SEARCH,
            {
                "jql": f"project = {PROJECT} AND (statusCategory != Done OR resolved >= -365d) "
                "ORDER BY updated DESC",
                "fields": tickets.TICKET_FIELDS,
                "limit": 5,
            },
        )
        data = payload(result)
        assert isinstance(data.get("issues"), list) and data["issues"], data
        parsed = tickets.parse_search(result.structured, result.text)
        for ticket in parsed:
            assert tickets.ISSUE_KEY.match(ticket.key)
            assert ticket.key.startswith(f"{PROJECT}-")
            assert ticket.summary
            assert ticket.status and ticket.status_category
            assert ticket.issue_type
            assert ticket.created is not None and ticket.updated is not None
            assert ticket.is_open or ticket.resolved is not None or ticket.resolution

        first = parsed[0]
        detail = payload(
            await client.call_tool(
                tickets.GET_ISSUE, {"issue_key": first.key, "fields": tickets.TICKET_FIELDS}
            )
        )
        assert tickets.parse_ticket(detail).key == first.key

        # A query our mock can't support must fail loudly on both, never return everything.
        bad = await client.call_tool(tickets.SEARCH, {"jql": "project = OPS AND (", "limit": 1})
        assert bad.is_error
    finally:
        await client.close()


async def test_mock_seed_ground_truth() -> None:
    """The seeded backlog the tickets agent's scenarios rely on (`make seed-tickets`)."""
    client = await connect("mock")
    try:
        result = await client.call_tool(
            tickets.SEARCH,
            {
                "jql": 'project = OPS AND labels = payment-service AND text ~ "500"',
                "fields": tickets.TICKET_FIELDS,
            },
        )
        found = tickets.parse_search(result.structured, result.text)
        ops12 = next(t for t in found if t.key == "OPS-12")
        assert ops12.is_open and ops12.summary == "payment-service DB connection timeouts"
        assert ops12.components == ["payments"]
        assert ops12.url and ops12.url.endswith("/browse/OPS-12")
        web = await client.call_tool(tickets.GET_ISSUE, {"issue_key": "WEB-1"})
        assert web.is_error  # outside ALLOWED_PROJECTS
    finally:
        await client.close()
