"""The server over MCP (in-process client) with the in-memory store: no Postgres needed."""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp import Client
from starlette.testclient import TestClient

from mock_tickets_mcp.config import ServerSettings
from mock_tickets_mcp.server import create_server
from mock_tickets_mcp.store import MemoryTicketStore
from tests.sample import NOW, issues

SETTINGS = ServerSettings(store="memory", allowed_projects=("OPS",), max_results=3)
READ_TOOLS = {"jira_search", "jira_get_issue"}
WRITE_TOOLS = {"jira_create_issue", "jira_update_issue", "jira_add_comment"}
#: Fields our tickets agent requests (see backend tickets agent).
AGENT_FIELDS = "summary,description,status,issuetype,priority,labels,components,created,updated,resolution,resolutiondate"


def server(settings: ServerSettings = SETTINGS, store: MemoryTicketStore | None = None) -> Any:
    return create_server(settings, store or MemoryTicketStore(issues()), clock=lambda: NOW)


async def call(tool: str, args: dict[str, Any], srv: Any = None) -> Any:
    async with Client(srv or server()) as client:
        return await client.call_tool(tool, args)


def error_text(result: Any) -> str:
    assert result.is_error
    return " ".join(block.text for block in result.content)


async def test_tool_names_match_mcp_atlassian() -> None:
    async with Client(server()) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == READ_TOOLS | WRITE_TOOLS
    assert all(tools[name].annotations.read_only_hint for name in READ_TOOLS)
    assert not any(tools[name].annotations.read_only_hint for name in WRITE_TOOLS)
    search_args = set(tools["jira_search"].input_schema["properties"])
    assert {"jql", "fields", "limit", "start_at", "projects_filter"} <= search_args
    create_args = set(tools["jira_create_issue"].input_schema["properties"])
    assert {
        "project_key",
        "summary",
        "issue_type",
        "description",
        "components",
        "additional_fields",
    } <= create_args


async def test_read_only_mode_hides_write_tools() -> None:
    settings = ServerSettings(store="memory", read_only=True)
    async with Client(server(settings)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == READ_TOOLS


async def test_search_shape_matches_mcp_atlassian() -> None:
    result = await call(
        "jira_search",
        {"jql": "project = OPS AND labels = payment-service ORDER BY key", "fields": AGENT_FIELDS},
    )
    data = result.structured_content
    assert set(data) == {"total", "start_at", "max_results", "issues"}
    assert data["total"] == 2
    ops12 = data["issues"][1]
    assert ops12["key"] == "OPS-12"
    assert ops12["summary"] == "payment-service DB connection timeouts"
    assert ops12["status"] == {"name": "Open", "category": "To Do", "color": "blue-gray"}
    assert ops12["issue_type"] == {"name": "Bug"}
    assert ops12["priority"] == {"name": "High"}
    assert ops12["labels"] == ["payment-service", "database", "known-issue"]
    assert ops12["components"] == ["payments"]
    assert ops12["created"] == "2026-09-22 10:30:00 UTC"
    assert ops12["browse_url"] == "http://localhost:8109/browse/OPS-12"
    assert "resolution" not in ops12  # unresolved
    ops2 = data["issues"][0]
    assert ops2["resolution"] == {"name": "Fixed"}
    assert ops2["resolutiondate"] == "2026-03-10T10:30:00.000+0000"


async def test_search_respects_fields_limit_and_paging() -> None:
    result = await call(
        "jira_search", {"jql": "project = OPS ORDER BY key", "fields": "summary", "limit": 50}
    )
    data = result.structured_content
    assert data["total"] == 4 and data["max_results"] == 3  # capped by MAX_RESULTS
    assert [i["key"] for i in data["issues"]] == ["OPS-2", "OPS-3", "OPS-7"]
    assert set(data["issues"][0]) == {"id", "key", "summary", "browse_url"}
    page2 = (
        await call("jira_search", {"jql": "project = OPS ORDER BY key", "start_at": 3})
    ).structured_content
    assert [i["key"] for i in page2["issues"]] == ["OPS-12"]


async def test_disallowed_projects_are_invisible() -> None:
    data = (await call("jira_search", {"jql": "labels = payment-service"})).structured_content
    assert "WEB-1" not in {i["key"] for i in data["issues"]}
    data = (
        await call("jira_search", {"jql": "labels = payment-service", "projects_filter": "WEB"})
    ).structured_content
    assert data["issues"] == []
    assert "does not exist" in error_text(await call("jira_get_issue", {"issue_key": "WEB-1"}))


async def test_unsupported_jql_is_a_clear_tool_error() -> None:
    result = await call("jira_search", {"jql": "assignee = currentUser()"})
    assert "Unsupported or invalid JQL" in error_text(result)


async def test_get_issue_with_comments() -> None:
    result = await call("jira_get_issue", {"issue_key": "OPS-7", "include": "comments"})
    data = result.structured_content
    assert data["key"] == "OPS-7" and data["status"]["name"] == "In Progress"
    assert data["comments"][0]["body"] == "Index migration drafted."
    assert (
        "invalid issue key"
        in error_text(await call("jira_get_issue", {"issue_key": "ops 7"})).casefold()
    )


async def test_create_update_comment_roundtrip() -> None:
    store = MemoryTicketStore(issues())
    srv = server(store=store)
    async with Client(srv) as client:
        created = await client.call_tool(
            "jira_create_issue",
            {
                "project_key": "OPS",
                "summary": "[aiops] payment-service HTTP 500s",
                "issue_type": "Bug",
                "description": "Findings...",
                "components": "payments",
                "additional_fields": json.dumps(
                    {"labels": ["aiops", "payment-service"], "priority": {"name": "High"}}
                ),
            },
        )
        issue = created.structured_content["issue"]
        assert created.structured_content["message"] == "Issue created successfully"
        assert issue["key"] == "OPS-13"  # next number in the project
        assert issue["labels"] == ["aiops", "payment-service"] and issue["priority"] == {
            "name": "High"
        }
        assert issue["reporter"]["display_name"] == "aiops-agent"

        updated = await client.call_tool(
            "jira_update_issue",
            {
                "issue_key": "OPS-13",
                "fields": json.dumps({"summary": "Renamed"}),
                "components": "payments,db",
            },
        )
        assert updated.structured_content["issue"]["summary"] == "Renamed"
        assert updated.structured_content["issue"]["components"] == ["payments", "db"]

        comment = await client.call_tool(
            "jira_add_comment", {"issue_key": "OPS-13", "body": "Investigation: inv-1"}
        )
        assert set(comment.structured_content) == {"id", "body", "created", "author"}

        bad = await client.call_tool(
            "jira_update_issue", {"issue_key": "OPS-13", "fields": json.dumps({"status": "Done"})}
        )
        assert "not supported by the mock" in error_text(bad)
        denied = await client.call_tool(
            "jira_create_issue", {"project_key": "WEB", "summary": "x", "issue_type": "Bug"}
        )
        assert "not allowed" in error_text(denied)
    stored = await store.get_issue("OPS-13")
    assert stored is not None and stored.comments[0].body == "Investigation: inv-1"


@pytest.mark.parametrize("path", ["/health", "/browse/OPS-12"])
def test_http_routes(path: str) -> None:
    app = server().streamable_http_app()
    with TestClient(app) as http:
        response = http.get(path)
    assert response.status_code == 200
    if path.startswith("/browse"):
        assert "payment-service DB connection timeouts" in response.text


def test_browse_unknown_issue_is_404() -> None:
    with TestClient(server().streamable_http_app()) as http:
        assert http.get("/browse/OPS-999").status_code == 404
