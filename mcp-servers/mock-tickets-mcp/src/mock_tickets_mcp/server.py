"""MCP tools with mcp-atlassian's Jira names and argument shapes (v0.23.x).

Read:  jira_search, jira_get_issue
Write: jira_create_issue, jira_update_issue, jira_add_comment (not registered in READ_ONLY_MODE)

Agents only ever get the read tools (capability allowlist); writes are executed by the
approval framework after a human approves them.
"""

from __future__ import annotations

import html
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from mock_tickets_mcp import jql as jql_engine
from mock_tickets_mcp.config import ServerSettings
from mock_tickets_mcp.models import (
    DEFAULT_READ_FIELDS,
    ISSUE_KEY,
    PROJECT_KEY_PATTERN,
    Issue,
    format_timestamp,
)
from mock_tickets_mcp.store import UPDATABLE, NewIssue, StoreError, TicketStore

log = logging.getLogger(__name__)

INSTRUCTIONS = """Offline Jira stand-in (mock) with the same tools as mcp-atlassian.
Use jira_search with JQL (project, status, statusCategory, labels, component, text ~ "...",
created/updated/resolved >= date, ORDER BY) and jira_get_issue for details."""

READ = ToolAnnotations(read_only_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
PROJECT_KEY = re.compile(PROJECT_KEY_PATTERN)

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _json_object(value: str | dict[str, Any] | None, name: str) -> dict[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except ValueError as exc:
        raise ToolError(f"{name} must be a JSON object string: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ToolError(f"{name} must be a JSON object")
    return parsed


def _names(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _changes(values: dict[str, Any]) -> dict[str, Any]:
    """Map Jira field updates onto store columns; reject what the mock can't do."""
    changes: dict[str, Any] = {}
    for name, value in values.items():
        if name == "priority":
            changes["priority"] = value.get("name") if isinstance(value, dict) else str(value)
        elif name == "labels":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ToolError("labels must be a list of strings")
            changes["labels"] = value
        elif name in ("summary", "description", "assignee"):
            changes[name] = None if value is None else str(value)
        else:
            supported = ", ".join(sorted(UPDATABLE - {"components"}))
            raise ToolError(f"Field '{name}' is not supported by the mock (supported: {supported})")
    return changes


def create_server(
    settings: ServerSettings, store: TicketStore, *, clock: Clock = _utcnow
) -> MCPServer:
    server = MCPServer("mock-tickets-mcp", instructions=INSTRUCTIONS, version="0.1.0")
    allowed = set(settings.allowed_projects)

    def visible(project: str) -> bool:
        return not allowed or project in allowed

    async def guarded[T](coro: Awaitable[T]) -> T:
        try:
            return await coro
        except StoreError as exc:
            raise ToolError(str(exc)) from exc

    async def load(issue_key: str) -> Issue:
        if not ISSUE_KEY.match(issue_key):
            raise ToolError(f"Invalid issue key '{issue_key}' (expected e.g. 'OPS-12')")
        issue = await guarded(store.get_issue(issue_key))
        if issue is None or not visible(issue.project):
            raise ToolError(
                f"Issue {issue_key} does not exist or you do not have permission to see it."
            )
        return issue

    # -- read ------------------------------------------------------------------------------

    @server.tool(annotations=READ)
    async def jira_search(
        jql: str,
        fields: str = DEFAULT_READ_FIELDS,
        limit: int = 10,
        start_at: int = 0,
        projects_filter: str | None = None,
        expand: str | None = None,
        page_token: str | None = None,
        use_display_names: bool = False,
    ) -> dict[str, Any]:
        """Search Jira issues using JQL (Jira Query Language).

        Supported subset: project, key, status, statusCategory, issuetype, priority, resolution,
        labels, component, text/summary/description ~ "words", created/updated/resolved with
        > >= < <= and 'YYYY-MM-DD' or '-7d', AND/OR/NOT, parentheses, ORDER BY.
        Example: project = OPS AND labels = payment-service AND statusCategory != Done
                 ORDER BY updated DESC

        Args:
            jql: JQL query string.
            fields: Comma-separated fields to return, or '*all'.
            limit: Maximum number of results (1-50).
            start_at: Starting index for pagination (0-based).
            projects_filter: Comma-separated project keys to restrict the search to.
            expand: Accepted for compatibility; ignored by the mock.
            page_token: Accepted for compatibility; ignored by the mock.
            use_display_names: Accepted for compatibility; ignored by the mock.
        """
        try:
            query = jql_parse(jql)
        except jql_engine.JQLError as exc:
            raise ToolError(f"Unsupported or invalid JQL: {exc}") from exc
        if limit < 1:
            raise ToolError("limit must be >= 1")
        if start_at < 0:
            raise ToolError("start_at must be >= 0")
        projects: set[str] | None = set(allowed) if allowed else None
        requested = {p.upper() for p in _names(projects_filter)}
        if requested:
            projects = requested & projects if projects is not None else requested
        if projects is not None and not projects:
            return {"total": 0, "start_at": start_at, "max_results": 0, "issues": []}
        issues = await guarded(
            store.list_issues(sorted(projects) if projects is not None else None)
        )
        try:
            hits = jql_engine.search(issues, query, clock())
        except jql_engine.JQLError as exc:
            raise ToolError(f"Unsupported or invalid JQL: {exc}") from exc
        size = min(limit, settings.max_results)
        page = hits[start_at : start_at + size]
        return {
            "total": len(hits),
            "start_at": start_at,
            "max_results": size,
            "issues": [i.to_simplified_dict(fields, base_url=settings.base_url) for i in page],
        }

    @server.tool(annotations=READ)
    async def jira_get_issue(
        issue_key: str,
        fields: str = DEFAULT_READ_FIELDS,
        expand: str | None = None,
        comment_limit: int = 10,
        properties: str | None = None,
        update_history: bool = True,
        include: str | None = None,
        use_display_names: bool = False,
    ) -> dict[str, Any]:
        """Get details of a specific Jira issue.

        Args:
            issue_key: Jira issue key (e.g. 'OPS-12').
            fields: Comma-separated fields to return (e.g. 'summary,status,labels'), or '*all'.
            expand: Accepted for compatibility; ignored by the mock.
            comment_limit: Maximum number of comments to include (0 for none).
            properties: Accepted for compatibility; ignored by the mock.
            update_history: Accepted for compatibility; ignored by the mock.
            include: Comma-separated sections to inline; 'comments' adds comments.
            use_display_names: Accepted for compatibility; ignored by the mock.
        """
        issue = await load(issue_key)
        requested = fields
        if "comments" in _names(include) and fields != "*all":
            requested = f"{fields},comment"
        issue.comments = issue.comments[-comment_limit:] if comment_limit > 0 else []
        return issue.to_simplified_dict(requested, base_url=settings.base_url)

    # -- write (only reachable through the approval executor) ------------------------------

    if not settings.read_only:

        @server.tool(annotations=WRITE)
        async def jira_create_issue(
            project_key: str,
            summary: str,
            issue_type: str,
            assignee: str | None = None,
            description: str | None = None,
            components: str | None = None,
            additional_fields: str | dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            """Create a new Jira issue.

            Args:
                project_key: The Jira project key (e.g. 'OPS').
                summary: Summary/title of the issue.
                issue_type: Issue type (e.g. 'Task', 'Bug', 'Story', 'Incident').
                assignee: Optional assignee identifier.
                description: Issue description in Markdown.
                components: Comma-separated component names (e.g. 'payments').
                additional_fields: JSON string, e.g. '{"labels": ["aiops"], "priority": {"name": "High"}}'.
            """
            if not PROJECT_KEY.match(project_key):
                raise ToolError(f"Invalid project key '{project_key}'")
            if not visible(project_key):
                raise ToolError(f"Project {project_key} does not exist or is not allowed.")
            if not summary.strip():
                raise ToolError("summary must not be empty")
            extra = _changes(_json_object(additional_fields, "additional_fields"))
            new = NewIssue(
                project=project_key,
                summary=summary.strip(),
                issue_type=issue_type,
                created=clock(),
                description=description,
                priority=extra.get("priority") or "Medium",
                labels=list(extra.get("labels") or []),
                components=_names(components),
                reporter=settings.author,
                assignee=assignee,
            )
            issue = await guarded(store.create_issue(new))
            log.info("created %s: %s", issue.key, issue.summary)
            return {
                "message": "Issue created successfully",
                "issue": issue.to_simplified_dict("*all", base_url=settings.base_url),
            }

        @server.tool(annotations=WRITE)
        async def jira_update_issue(
            issue_key: str,
            fields: str | dict[str, Any],
            additional_fields: str | dict[str, Any] | None = None,
            components: str | None = None,
            attachments: str | None = None,
            return_fields: str = "*all",
        ) -> dict[str, Any]:
            """Update an existing Jira issue.

            Args:
                issue_key: Jira issue key (e.g. 'OPS-12').
                fields: JSON string of fields to update, e.g. '{"summary": "New", "labels": ["a"]}'.
                additional_fields: Optional JSON string of more fields (same keys as fields).
                components: Optional comma-separated component names.
                attachments: Not supported by the mock.
                return_fields: Fields of the updated issue to return, or '*all'.
            """
            if attachments:
                raise ToolError("attachments are not supported by the mock")
            await load(issue_key)
            changes = _changes(
                {
                    **_json_object(fields, "fields"),
                    **_json_object(additional_fields, "additional_fields"),
                }
            )
            if components is not None:
                changes["components"] = _names(components)
            if not changes:
                raise ToolError("nothing to update")
            issue = await guarded(store.update_issue(issue_key, changes, clock()))
            return {
                "message": "Issue updated successfully",
                "issue": issue.to_simplified_dict(return_fields, base_url=settings.base_url),
            }

        @server.tool(annotations=WRITE)
        async def jira_add_comment(
            issue_key: str,
            body: str,
            visibility: str | None = None,
            public: bool | None = None,
        ) -> dict[str, Any]:
            """Add a comment to a Jira issue.

            Args:
                issue_key: Jira issue key (e.g. 'OPS-12').
                body: Comment text in Markdown.
                visibility: Accepted for compatibility; ignored by the mock.
                public: Accepted for compatibility; ignored by the mock.
            """
            await load(issue_key)
            if not body.strip():
                raise ToolError("body must not be empty")
            comment = await guarded(store.add_comment(issue_key, body, settings.author, clock()))
            return {
                "id": comment.id,
                "body": comment.body,
                "created": format_timestamp(comment.created),
                "author": comment.author,
            }

    # -- plain HTTP: a minimal issue page so evidence links open somewhere --------------------

    @server.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
    async def health(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @server.custom_route("/browse/{key}", methods=["GET"])  # type: ignore[untyped-decorator]
    async def browse(request: Request) -> Response:
        key = str(request.path_params.get("key", ""))
        try:
            issue = await load(key)
        except ToolError as exc:
            return HTMLResponse(f"<p>{html.escape(str(exc))}</p>", status_code=404)
        rows = "".join(
            f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>"
            for k, v in issue.to_simplified_dict("*all").items()
            if k not in ("description", "comments")
        )
        comments = "".join(
            f"<li><b>{html.escape(c.author)}</b> {format_timestamp(c.created)}"
            f"<pre>{html.escape(c.body)}</pre></li>"
            for c in issue.comments
        )
        body = (
            f"<h1>{html.escape(issue.key)}: {html.escape(issue.summary)}</h1>"
            f"<p><i>mock-tickets-mcp (offline Jira stand-in)</i></p><table>{rows}</table>"
            f"<h2>Description</h2><pre>{html.escape(issue.description or '')}</pre>"
            f"<h2>Comments</h2><ul>{comments}</ul>"
        )
        return HTMLResponse(f"<!doctype html><title>{html.escape(issue.key)}</title>{body}")

    return server


def jql_parse(text: str) -> jql_engine.Query:
    if not text.strip():
        raise jql_engine.JQLError("jql must not be empty")
    return jql_engine.parse(text)
