"""Parsing of mcp-atlassian shaped ticket payloads (mock and Jira Cloud variants)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aiops.core.config import load_settings
from aiops.mcp.tickets import (
    READ_TOOLS,
    WRITE_TOOLS,
    as_payload,
    parse_search,
    parse_ticket,
    parse_timestamp,
)
from aiops.seed.tickets import TICKETS, rows


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-09-25 10:08:00 UTC", datetime(2026, 9, 25, 10, 8, tzinfo=UTC)),
        ("2026-09-25T10:08:00.000+0000", datetime(2026, 9, 25, 10, 8, tzinfo=UTC)),
        ("2026-09-25T12:08:00.000+0200", datetime(2026, 9, 25, 10, 8, tzinfo=UTC)),
        ("2026-09-25T10:08:00Z", datetime(2026, 9, 25, 10, 8, tzinfo=UTC)),
        ("2026-09-25 12:08:00 CEST", None),  # ambiguous display timezone -> unknown
        ("", None),
        (None, None),
    ],
)
def test_parse_timestamp(value: object, expected: datetime | None) -> None:
    assert parse_timestamp(value) == expected


def test_parse_mcp_atlassian_issue() -> None:
    # Shape of JiraIssue.to_simplified_dict (mcp-atlassian 0.23.1) on Jira Cloud.
    raw = {
        "id": "10012",
        "key": "OPS-12",
        "summary": "payment-service DB connection timeouts",
        "url": "https://example.atlassian.net/rest/api/2/issue/10012",
        "browse_url": "https://example.atlassian.net/browse/OPS-12",
        "description": "HTTP 500",
        "status": {"name": "Done", "category": "Done", "color": "green"},
        "issue_type": {"name": "Bug"},
        "priority": {"name": "High"},
        "resolution": {"name": "Fixed", "id": "10000"},
        "resolutiondate": "2026-09-24T10:00:00.000+0000",
        "assignee": {"display_name": "Unassigned"},
        "labels": ["payment-service"],
        "components": ["payments"],
        "created": "2026-09-22 10:30:00 UTC",
        "updated": "2026-09-24 10:00:00 UTC",
    }
    ticket = parse_ticket(raw)
    assert ticket.key == "OPS-12" and ticket.status == "Done" and not ticket.is_open
    assert ticket.issue_type == "Bug" and ticket.priority == "High"
    assert ticket.resolution == "Fixed"
    assert ticket.resolved == datetime(2026, 9, 24, 10, tzinfo=UTC)
    assert ticket.url == "https://example.atlassian.net/browse/OPS-12"
    assert ticket.components == ["payments"]


def test_parse_search_from_json_text() -> None:
    text = json.dumps({"total": 1, "issues": [{"key": "OPS-1", "status": {"name": "Open"}}]})
    [ticket] = parse_search(None, text)
    assert ticket.key == "OPS-1" and ticket.is_open  # no category, no resolution -> open
    assert as_payload(None, "not json") == {}
    assert parse_search({"issues": [{"summary": "no key"}]}) == []


def test_seed_backlog_ground_truth() -> None:
    now = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
    by_key = {r["key"]: r for r in rows(now)}
    assert len(by_key) == len(TICKETS)
    ops12 = by_key["OPS-12"]
    assert ops12["status_category"] == "To Do" and "HTTP 500" in ops12["description"]
    assert by_key["OPS-3"]["resolved"] == datetime(2026, 8, 12, 10, 30, tzinfo=UTC)
    assert by_key["OPS-7"]["updated"] == datetime(2026, 9, 24, 10, 30, tzinfo=UTC)  # comment
    assert {r["project"] for r in by_key.values()} == {"OPS", "WEB"}


def test_local_tickets_capability_is_read_only_mock(repo_config_dir: Path) -> None:
    settings = load_settings("local", repo_config_dir)
    cap = settings.capability("tickets")
    assert cap.provider == "mock"
    assert cap.mcp.url == "http://localhost:8109/mcp"
    assert sorted(cap.tool_allowlist) == sorted(READ_TOOLS)
    assert not set(cap.tool_allowlist) & set(WRITE_TOOLS)
    assert cap.settings["project_key"] == "OPS"
    assert cap.settings["ui_link_template"].format(key="OPS-12").endswith("/browse/OPS-12")


def test_switching_to_jira_is_configuration_only(
    repo_config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TICKETS_PROVIDER", "jira")
    monkeypatch.setenv("TICKETS_MCP_URL", "http://localhost:8102/mcp")
    monkeypatch.setenv("TICKETS_UI_URL", "https://example.atlassian.net")
    cap = load_settings("local", repo_config_dir).capability("tickets")
    assert cap.provider == "jira" and cap.mcp.url == "http://localhost:8102/mcp"
    link = cap.settings["ui_link_template"].format(key="OPS-12")
    assert link == "https://example.atlassian.net/browse/OPS-12"
