"""The ``tickets`` capability contract: mcp-atlassian's Jira tools and result shapes.

Both providers speak it: ``mock`` (our mock-tickets-mcp) and ``jira`` (sooperset/mcp-atlassian
0.23.x on a Jira Cloud site). Everything that reads tickets parses them through here, and
the contract test checks both servers against it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

SEARCH = "jira_search"
GET_ISSUE = "jira_get_issue"
CREATE_ISSUE = "jira_create_issue"
ADD_COMMENT = "jira_add_comment"
UPDATE_ISSUE = "jira_update_issue"
READ_TOOLS = (SEARCH, GET_ISSUE)
WRITE_TOOLS = (CREATE_ISSUE, ADD_COMMENT, UPDATE_ISSUE)

#: Fields requested by readers (mcp-atlassian's defaults omit components/resolution).
TICKET_FIELDS = (
    "summary,description,status,issuetype,priority,labels,components,"
    "created,updated,resolution,resolutiondate"
)
ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
DONE_CATEGORIES = {"done", "complete", "completed"}


class Ticket(BaseModel):
    """A ticket as agents see it, parsed from ``to_simplified_dict`` output."""

    model_config = ConfigDict(extra="forbid")

    key: str
    summary: str = ""
    description: str = ""
    status: str = ""
    status_category: str = ""
    issue_type: str = ""
    priority: str | None = None
    labels: list[str] = Field(default_factory=list)
    components: list[str] = Field(default_factory=list)
    resolution: str | None = None
    created: datetime | None = None
    updated: datetime | None = None
    resolved: datetime | None = None
    url: str | None = None

    @property
    def is_open(self) -> bool:
        if self.status_category:
            return self.status_category.casefold() not in DONE_CATEGORIES
        return self.resolution is None

    def text(self) -> str:
        return f"{self.summary}\n{self.description}"


def parse_timestamp(value: Any) -> datetime | None:
    """Jira REST ('2026-09-25T10:08:00.000+0000'), ISO, or mcp-atlassian's
    '2026-09-25 10:08:00 UTC' display format. Unknown formats -> None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for suffix in (" UTC", " GMT", " Z"):
        if text.endswith(suffix):
            text = text[: -len(suffix)] + "+00:00"
            break
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)  # +0000 -> +00:00
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def _names(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [n for n in (_name(v) for v in values) if n]


def parse_ticket(raw: dict[str, Any]) -> Ticket:
    status = raw.get("status")
    category = status.get("category") if isinstance(status, dict) else None
    resolution = _name(raw.get("resolution")) or None
    return Ticket(
        key=str(raw["key"]),
        summary=str(raw.get("summary") or ""),
        description=str(raw.get("description") or ""),
        status=_name(status),
        status_category=str(category or ""),
        issue_type=_name(raw.get("issue_type")),
        priority=_name(raw.get("priority")) or None,
        labels=[str(label) for label in raw.get("labels") or []],
        components=_names(raw.get("components")),
        resolution=resolution,
        created=parse_timestamp(raw.get("created")),
        updated=parse_timestamp(raw.get("updated")),
        resolved=parse_timestamp(raw.get("resolutiondate")),
        url=raw.get("browse_url"),  # "url" is the REST self link, not a UI link
    )


def as_payload(data: Any, text: str = "") -> dict[str, Any]:
    """Tool result -> dict: structured content, or the JSON text mcp-atlassian returns."""
    if isinstance(data, dict):
        return data
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def parse_search(data: Any, text: str = "") -> list[Ticket]:
    issues = as_payload(data, text).get("issues") or []
    return [parse_ticket(i) for i in issues if isinstance(i, dict) and i.get("key")]
