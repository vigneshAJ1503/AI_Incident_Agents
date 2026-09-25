"""Ticket storage: Postgres schema ``tickets`` (psycopg 3, async) or in-memory (tests).

The Postgres store owns the schema and creates it idempotently on first use.
Searches load the (small) project backlog and evaluate JQL in Python, so both stores
share exactly the same JQL semantics.
"""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from mock_tickets_mcp.models import Comment, Issue


class StoreError(Exception):
    """Storage failure; the message is shown to the caller."""


@dataclass
class NewIssue:
    project: str
    summary: str
    issue_type: str
    created: datetime
    description: str | None = None
    priority: str | None = "Medium"
    labels: list[str] = field(default_factory=list)
    components: list[str] = field(default_factory=list)
    reporter: str | None = None
    assignee: str | None = None


#: Fields jira_update_issue may change.
UPDATABLE = {"summary", "description", "priority", "labels", "components", "assignee"}


class TicketStore(Protocol):
    async def list_issues(self, projects: Sequence[str] | None) -> list[Issue]: ...

    async def get_issue(self, key: str) -> Issue | None: ...

    async def create_issue(self, new: NewIssue) -> Issue: ...

    async def update_issue(self, key: str, changes: dict[str, Any], now: datetime) -> Issue: ...

    async def add_comment(self, key: str, body: str, author: str, now: datetime) -> Comment: ...

    async def close(self) -> None: ...


# --------------------------------------------------------------------------- memory


class MemoryTicketStore:
    """In-memory store for tests and demos."""

    def __init__(self, issues: Iterable[Issue] = ()) -> None:
        self._issues: dict[str, Issue] = {i.key: copy.deepcopy(i) for i in issues}
        self._comment_seq = 0

    async def list_issues(self, projects: Sequence[str] | None) -> list[Issue]:
        wanted = {p.upper() for p in projects} if projects else None
        return [
            copy.deepcopy(i) for i in self._issues.values() if wanted is None or i.project in wanted
        ]

    async def get_issue(self, key: str) -> Issue | None:
        issue = self._issues.get(key)
        return copy.deepcopy(issue) if issue else None

    async def create_issue(self, new: NewIssue) -> Issue:
        numbers = [i.number for i in self._issues.values() if i.project == new.project]
        key = f"{new.project}-{max(numbers, default=0) + 1}"
        issue = Issue(
            key=key,
            summary=new.summary,
            issue_type=new.issue_type,
            status="Open",
            status_category="To Do",
            created=new.created,
            updated=new.created,
            description=new.description,
            priority=new.priority,
            labels=list(new.labels),
            components=list(new.components),
            reporter=new.reporter,
            assignee=new.assignee,
        )
        self._issues[key] = issue
        return copy.deepcopy(issue)

    async def update_issue(self, key: str, changes: dict[str, Any], now: datetime) -> Issue:
        issue = self._issues.get(key)
        if issue is None:
            raise StoreError(f"Issue {key} does not exist")
        unknown = set(changes) - UPDATABLE
        if unknown:
            raise StoreError(f"Cannot update fields {sorted(unknown)}")
        for name, value in changes.items():
            setattr(issue, name, value)
        issue.updated = now
        return copy.deepcopy(issue)

    async def add_comment(self, key: str, body: str, author: str, now: datetime) -> Comment:
        issue = self._issues.get(key)
        if issue is None:
            raise StoreError(f"Issue {key} does not exist")
        self._comment_seq += 1
        comment = Comment(id=str(self._comment_seq), body=body, author=author, created=now)
        issue.comments.append(comment)
        issue.updated = now
        return comment

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------------- postgres

DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.issues (
    key             text PRIMARY KEY,
    project         text NOT NULL,
    number          integer NOT NULL,
    summary         text NOT NULL,
    description     text,
    issue_type      text NOT NULL,
    status          text NOT NULL,
    status_category text NOT NULL,
    priority        text,
    resolution      text,
    labels          text[] NOT NULL DEFAULT '{{}}',
    components      text[] NOT NULL DEFAULT '{{}}',
    reporter        text,
    assignee        text,
    created         timestamptz NOT NULL,
    updated         timestamptz NOT NULL,
    resolved        timestamptz,
    UNIQUE (project, number)
);
CREATE TABLE IF NOT EXISTS {schema}.comments (
    id        bigserial PRIMARY KEY,
    issue_key text NOT NULL REFERENCES {schema}.issues(key) ON DELETE CASCADE,
    body      text NOT NULL,
    author    text NOT NULL,
    created   timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS comments_issue_key_idx ON {schema}.comments (issue_key);
"""

_COLUMNS = (
    "key, summary, description, issue_type, status, status_category, priority, resolution, "
    "labels, components, reporter, assignee, created, updated, resolved"
)


def _issue(row: dict[str, Any], comments: list[Comment]) -> Issue:
    return Issue(
        key=row["key"],
        summary=row["summary"],
        description=row["description"],
        issue_type=row["issue_type"],
        status=row["status"],
        status_category=row["status_category"],
        priority=row["priority"],
        resolution=row["resolution"],
        labels=list(row["labels"] or []),
        components=list(row["components"] or []),
        reporter=row["reporter"],
        assignee=row["assignee"],
        created=row["created"],
        updated=row["updated"],
        resolved=row["resolved"],
        comments=comments,
    )


def _comment(row: dict[str, Any]) -> Comment:
    return Comment(
        id=str(row["id"]), body=row["body"], author=row["author"], created=row["created"]
    )


class PostgresTicketStore:
    """Postgres-backed store. A short-lived connection per operation (low traffic mock)."""

    def __init__(self, dsn: str, schema: str = "tickets", *, connect_timeout_s: int = 5) -> None:
        self._dsn = dsn
        self._schema = schema
        self._connect_timeout_s = connect_timeout_s
        self._ready = False
        self._lock = asyncio.Lock()

    def _q(self, template: str) -> sql.Composed:
        return sql.SQL(template).format(schema=sql.Identifier(self._schema))

    async def _connect(self) -> psycopg.AsyncConnection[dict[str, Any]]:
        try:
            return await psycopg.AsyncConnection.connect(
                self._dsn, connect_timeout=self._connect_timeout_s, row_factory=dict_row
            )
        except psycopg.Error as exc:
            raise StoreError(f"Postgres is not reachable: {exc}") from exc

    async def setup(self) -> None:
        """Create the schema once (idempotent; serialized with an advisory lock)."""
        if self._ready:
            return
        async with self._lock:
            if self._ready:
                return
            async with await self._connect() as conn:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext('mock-tickets-ddl'))")
                await conn.execute(self._q(DDL))
            self._ready = True

    async def _fetch(self, query: sql.Composed, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        await self.setup()
        try:
            async with await self._connect() as conn:
                cursor = await conn.execute(query, params)
                return await cursor.fetchall()
        except psycopg.Error as exc:
            raise StoreError(f"Database error: {exc}") from exc

    async def _comments(self, keys: list[str]) -> dict[str, list[Comment]]:
        by_key: dict[str, list[Comment]] = {k: [] for k in keys}
        if not keys:
            return by_key
        rows = await self._fetch(
            self._q(
                "SELECT id, issue_key, body, author, created FROM {schema}.comments "
                "WHERE issue_key = ANY(%s) ORDER BY created, id"
            ),
            [keys],
        )
        for row in rows:
            by_key[row["issue_key"]].append(_comment(row))
        return by_key

    async def list_issues(self, projects: Sequence[str] | None) -> list[Issue]:
        if projects:
            rows = await self._fetch(
                self._q(f"SELECT {_COLUMNS} FROM {{schema}}.issues WHERE project = ANY(%s)"),  # noqa: S608
                [[p.upper() for p in projects]],
            )
        else:
            rows = await self._fetch(self._q(f"SELECT {_COLUMNS} FROM {{schema}}.issues"))  # noqa: S608
        comments = await self._comments([r["key"] for r in rows])
        return [_issue(r, comments[r["key"]]) for r in rows]

    async def get_issue(self, key: str) -> Issue | None:
        rows = await self._fetch(
            self._q(f"SELECT {_COLUMNS} FROM {{schema}}.issues WHERE key = %s"),  # noqa: S608
            [key],
        )
        if not rows:
            return None
        return _issue(rows[0], (await self._comments([key]))[key])

    async def create_issue(self, new: NewIssue) -> Issue:
        await self.setup()
        try:
            async with await self._connect() as conn, conn.transaction():
                # Serialize key allocation per project.
                await conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", [new.project])
                cursor = await conn.execute(
                    self._q(
                        "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM {schema}.issues WHERE project = %s"
                    ),
                    [new.project],
                )
                row = await cursor.fetchone()
                number = int(row["n"]) if row else 1
                key = f"{new.project}-{number}"
                await conn.execute(
                    self._q(
                        "INSERT INTO {schema}.issues (key, project, number, summary, description, "
                        "issue_type, status, status_category, priority, labels, components, reporter, "
                        "assignee, created, updated) VALUES "
                        "(%s, %s, %s, %s, %s, %s, 'Open', 'To Do', %s, %s, %s, %s, %s, %s, %s)"
                    ),
                    [
                        key,
                        new.project,
                        number,
                        new.summary,
                        new.description,
                        new.issue_type,
                        new.priority,
                        new.labels,
                        new.components,
                        new.reporter,
                        new.assignee,
                        new.created,
                        new.created,
                    ],
                )
        except psycopg.Error as exc:
            raise StoreError(f"Database error: {exc}") from exc
        issue = await self.get_issue(key)
        if issue is None:  # pragma: no cover - just inserted
            raise StoreError(f"Issue {key} vanished after insert")
        return issue

    async def update_issue(self, key: str, changes: dict[str, Any], now: datetime) -> Issue:
        unknown = set(changes) - UPDATABLE
        if unknown:
            raise StoreError(f"Cannot update fields {sorted(unknown)}")
        assignments = [sql.SQL("{} = %s").format(sql.Identifier(name)) for name in sorted(changes)]
        assignments.append(sql.SQL("updated = %s").format())
        query = sql.SQL("UPDATE {schema}.issues SET {sets} WHERE key = %s RETURNING key").format(
            schema=sql.Identifier(self._schema), sets=sql.SQL(", ").join(assignments)
        )
        rows = await self._fetch(query, [*(changes[n] for n in sorted(changes)), now, key])
        if not rows:
            raise StoreError(f"Issue {key} does not exist")
        issue = await self.get_issue(key)
        if issue is None:  # pragma: no cover
            raise StoreError(f"Issue {key} does not exist")
        return issue

    async def add_comment(self, key: str, body: str, author: str, now: datetime) -> Comment:
        if await self.get_issue(key) is None:
            raise StoreError(f"Issue {key} does not exist")
        rows = await self._fetch(
            self._q(
                "INSERT INTO {schema}.comments (issue_key, body, author, created) "
                "VALUES (%s, %s, %s, %s) RETURNING id, body, author, created"
            ),
            [key, body, author, now],
        )
        await self._fetch(
            self._q("UPDATE {schema}.issues SET updated = %s WHERE key = %s RETURNING key"),
            [now, key],
        )
        return _comment(rows[0])

    async def close(self) -> None:
        return None
