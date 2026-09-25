"""Issue model and its mcp-atlassian compatible JSON shape.

``to_simplified_dict`` mirrors ``JiraIssue.to_simplified_dict`` of mcp-atlassian
0.23.x, so agents parse the mock and a real Jira Cloud site with the same code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

#: Same patterns as mcp-atlassian (servers/jira.py).
ISSUE_KEY_PATTERN = r"^[A-Z][A-Z0-9_]+-\d+$"
PROJECT_KEY_PATTERN = r"^[A-Z][A-Z0-9_]+$"
ISSUE_KEY = re.compile(ISSUE_KEY_PATTERN)

#: mcp-atlassian's DEFAULT_READ_JIRA_FIELDS.
DEFAULT_READ_FIELDS = "summary,description,status,assignee,reporter,labels,versions,priority,created,updated,issuetype"

#: Jira status categories (name -> colour), as returned by the REST API.
STATUS_CATEGORY_COLOURS = {"To Do": "blue-gray", "In Progress": "yellow", "Done": "green"}


def format_timestamp(ts: datetime) -> str:
    """mcp-atlassian's ``format_timestamp`` in a UTC container: '2026-09-25 10:08:00 UTC'."""
    return ts.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def jira_datetime(ts: datetime) -> str:
    """Raw Jira REST datetime, e.g. '2026-09-25T10:08:00.000+0000'."""
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def user_dict(name: str | None) -> dict[str, Any]:
    if not name:
        return {"display_name": "Unassigned"}
    return {
        "account_id": f"mock-{name}",
        "display_name": name,
        "name": name,
        "email": None,
        "avatar_url": None,
    }


@dataclass
class Comment:
    id: str
    body: str
    author: str
    created: datetime

    def to_simplified_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "body": self.body,
            "author": user_dict(self.author),
            "created": jira_datetime(self.created),
        }


@dataclass
class Issue:
    key: str
    summary: str
    issue_type: str
    status: str
    status_category: str  # To Do | In Progress | Done
    created: datetime
    updated: datetime
    description: str | None = None
    priority: str | None = "Medium"
    resolution: str | None = None
    resolved: datetime | None = None
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    reporter: str | None = None
    assignee: str | None = None
    comments: list[Comment] = field(default_factory=list)

    @property
    def project(self) -> str:
        return self.key.rsplit("-", 1)[0]

    @property
    def number(self) -> int:
        return int(self.key.rsplit("-", 1)[1])

    @property
    def id(self) -> str:
        """Stable numeric-looking id, like Jira's."""
        return str(10000 + self.number)

    def searchable_text(self) -> str:
        return " ".join([self.summary, self.description or "", *(c.body for c in self.comments)])

    def to_simplified_dict(
        self, fields: str | list[str] = DEFAULT_READ_FIELDS, *, base_url: str | None = None
    ) -> dict[str, Any]:
        requested = parse_fields(fields)

        def want(name: str) -> bool:
            return requested == "*all" or name in requested

        result: dict[str, Any] = {"id": self.id, "key": self.key}
        if want("summary"):
            result["summary"] = self.summary
        if base_url:
            result["browse_url"] = f"{base_url.rstrip('/')}/browse/{self.key}"
        if self.description and want("description"):
            result["description"] = self.description
        if want("status"):
            result["status"] = {
                "name": self.status,
                "category": self.status_category,
                "color": STATUS_CATEGORY_COLOURS.get(self.status_category, "medium-gray"),
            }
        if want("issuetype"):
            result["issue_type"] = {"name": self.issue_type}
        if self.priority and want("priority"):
            result["priority"] = {"name": self.priority}
        if want("project"):
            result["project"] = {"key": self.project, "name": self.project}
        if self.resolution and want("resolution"):
            result["resolution"] = {"name": self.resolution}
        if self.resolved and want("resolutiondate"):
            result["resolutiondate"] = jira_datetime(self.resolved)
        if want("assignee"):
            result["assignee"] = user_dict(self.assignee)
        if self.reporter and want("reporter"):
            result["reporter"] = user_dict(self.reporter)
        if self.labels and want("labels"):
            result["labels"] = list(self.labels)
        if self.components and want("components"):
            result["components"] = list(self.components)
        if want("created"):
            result["created"] = format_timestamp(self.created)
        if want("updated"):
            result["updated"] = format_timestamp(self.updated)
        if self.comments and want("comment"):
            result["comments"] = [c.to_simplified_dict() for c in self.comments]
        return result


def parse_fields(fields: str | list[str] | None) -> str | set[str]:
    """'summary, status' -> {'summary', 'status'}; '*all' stays '*all'."""
    if fields is None or fields == "":
        fields = DEFAULT_READ_FIELDS
    if fields == "*all":
        return "*all"
    items = fields.split(",") if isinstance(fields, str) else fields
    return {f.strip() for f in items if f.strip()}
