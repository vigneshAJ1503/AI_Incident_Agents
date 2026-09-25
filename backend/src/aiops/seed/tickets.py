"""Deterministic ticket backlog for the mock tickets MCP (schema ``tickets`` in Postgres).

The backlog is anchored at ``now`` so "open for 3 days" / "resolved 45 days ago" stay true
whenever it is seeded. Ground truth used by the scenarios:
  * OPS-12  open known issue: payment-service DB connection timeouts, HTTP 500s    (S1)
  * OPS-3   resolved incident: order-service OOMKilled                              (S2)
  * OPS-7   in-progress bug: inventory-service slow query, order-service timeouts  (S3)
  * OPS-5   resolved incident: payment-service slow during a Redis failover        (S5)
  * OPS-2   resolved payment pool incident, but 200 days old (outside look-backs)
  * noise: unrelated tasks/stories/bugs, and a WEB project outside the allowed scope

mock-tickets-mcp owns the schema (creates it on start); this module only fills it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import psycopg
from psycopg import sql

SEEDED_PROJECTS = ("OPS", "WEB")


@dataclass(frozen=True)
class SeedTicket:
    key: str
    summary: str
    issue_type: str
    status: str
    status_category: str  # To Do | In Progress | Done
    created_days_ago: float
    description: str = ""
    priority: str = "Medium"
    labels: tuple[str, ...] = ()
    components: tuple[str, ...] = ()
    resolution: str | None = None
    resolved_days_ago: float | None = None
    reporter: str = "sre-bot"
    assignee: str | None = None
    comments: tuple[tuple[str, str, float], ...] = field(default=())  # (author, body, days ago)

    @property
    def project(self) -> str:
        return self.key.rsplit("-", 1)[0]

    @property
    def number(self) -> int:
        return int(self.key.rsplit("-", 1)[1])


def _done(**kwargs: Any) -> dict[str, Any]:
    return {"status": "Done", "status_category": "Done", "resolution": "Fixed", **kwargs}


def _todo(**kwargs: Any) -> dict[str, Any]:
    return {"status": "Open", "status_category": "To Do", **kwargs}


TICKETS: tuple[SeedTicket, ...] = (
    SeedTicket(
        key="OPS-1",
        summary="Set up on-call rotation for the payments team",
        issue_type="Task",
        created_days_ago=180,
        description="Create the PagerDuty schedule and escalation policy.",
        labels=("process",),
        **_done(resolved_days_ago=170, resolution="Done"),
    ),
    SeedTicket(
        key="OPS-2",
        summary="payment-service connection pool exhausted during peak traffic",
        issue_type="Incident",
        created_days_ago=200,
        description=(
            "Black Friday peak: payment-service ran out of database connections and requests "
            "failed with connection timeouts. Pool size raised from 10 to 20."
        ),
        priority="High",
        labels=("payment-service", "database", "incident"),
        components=("payments",),
        **_done(resolved_days_ago=199),
    ),
    SeedTicket(
        key="OPS-3",
        summary="order-service OOMKilled after bulk export release",
        issue_type="Incident",
        created_days_ago=45,
        description=(
            "After v2.3.0 order-service pods were OOMKilled repeatedly (memory leak in the "
            "export cache). Restarts caused HTTP 503s for about 20 minutes. Fixed in v2.3.1 by "
            "bounding the cache; memory limit unchanged."
        ),
        priority="High",
        labels=("order-service", "memory", "oom", "incident"),
        components=("orders",),
        assignee="alex",
        comments=(("alex", "Postmortem: https://wiki.example.com/pm/ops-3", 44),),
        **_done(resolved_days_ago=44),
    ),
    SeedTicket(
        key="OPS-4",
        summary="Add dark mode to the admin dashboard",
        issue_type="Story",
        created_days_ago=60,
        description="Design has approved the palette.",
        priority="Low",
        labels=("frontend",),
        **_todo(),
    ),
    SeedTicket(
        key="OPS-5",
        summary="payment-service slow responses during Redis failover",
        issue_type="Incident",
        created_days_ago=60,
        description=(
            "Redis primary failover left the cache cold; all reads went to Postgres and "
            "payment-service became slow (p95 4 s) for 15 minutes. Action: cache warm-up job "
            "and a circuit breaker on the Redis client."
        ),
        priority="High",
        labels=("payment-service", "redis", "cache", "incident"),
        components=("payments",),
        **_done(resolved_days_ago=59.5),
    ),
    SeedTicket(
        key="OPS-6",
        summary="Upgrade Postgres minor version to 16.4",
        issue_type="Task",
        created_days_ago=30,
        description="Rolling upgrade during the maintenance window.",
        labels=("database", "maintenance"),
        **_done(resolved_days_ago=20, resolution="Done"),
    ),
    SeedTicket(
        key="OPS-7",
        summary="inventory-service slow query on stock reservation (missing index)",
        issue_type="Bug",
        status="In Progress",
        status_category="In Progress",
        created_days_ago=8,
        description=(
            "SELECT on stock_reservations does a sequential scan; p95 up to 3 s under load. "
            "order-service reservation calls hit timeouts when inventory-service is slow."
        ),
        priority="High",
        labels=("inventory-service", "slow-query", "database"),
        components=("inventory",),
        assignee="dana",
        comments=(("dana", "Index migration drafted, needs review.", 1),),
    ),
    SeedTicket(
        key="OPS-8",
        summary="user-service: add rate limiting to the login endpoint",
        issue_type="Story",
        created_days_ago=14,
        description="Protect against credential stuffing; 10 attempts per minute per IP.",
        labels=("user-service", "security"),
        components=("identity",),
        **_todo(),
    ),
    SeedTicket(
        key="OPS-9",
        summary="order-service: typo in the order confirmation email",
        issue_type="Bug",
        created_days_ago=5,
        description="'Thank you for you order' in the English template.",
        priority="Low",
        labels=("order-service",),
        components=("orders",),
        **_todo(),
    ),
    SeedTicket(
        key="OPS-10",
        summary="Rotate TLS certificates for the public ingress",
        issue_type="Task",
        created_days_ago=40,
        description="Certificates expire next month.",
        labels=("infra",),
        **_done(resolved_days_ago=38, resolution="Done"),
    ),
    SeedTicket(
        key="OPS-11",
        summary="user-service avatar upload fails for PNG files over 5 MB",
        issue_type="Bug",
        created_days_ago=25,
        description="Upload returns HTTP 413; raise the body size limit on the ingress.",
        labels=("user-service",),
        components=("identity",),
        **_done(resolved_days_ago=21),
    ),
    SeedTicket(
        key="OPS-12",
        summary="payment-service DB connection timeouts",
        issue_type="Bug",
        created_days_ago=3,
        description=(
            "Intermittent 'Database connection timeout: could not acquire a connection from the "
            "pool' errors on /api/v1/pay. Affected requests fail with HTTP 500. Suspected "
            "connection pool sizing; see OPS-2 for a similar incident. Workaround: scale out "
            "payment-service."
        ),
        priority="High",
        labels=("payment-service", "database", "known-issue"),
        components=("payments",),
        reporter="sam",
        assignee="sam",
        comments=(("sam", "Seen again overnight, 40 failed payments.", 2),),
        **_todo(),
    ),
    SeedTicket(
        key="OPS-13",
        summary="Document the payment-service refund flow",
        issue_type="Task",
        created_days_ago=12,
        description="Sequence diagram and failure modes for refunds.",
        priority="Low",
        labels=("payment-service", "docs"),
        components=("payments",),
        **_todo(),
    ),
    SeedTicket(
        key="OPS-14",
        summary="Migrate CI pipelines to GitHub Actions",
        issue_type="Story",
        created_days_ago=90,
        description="Replace the legacy Jenkins jobs.",
        labels=("ci",),
        **_done(resolved_days_ago=70, resolution="Done"),
    ),
    SeedTicket(
        key="OPS-15",
        summary="inventory-service: stock count off by one after cancellation",
        issue_type="Bug",
        created_days_ago=15,
        description="Cancelled orders release one item too many.",
        labels=("inventory-service",),
        components=("inventory",),
        **_done(resolved_days_ago=12),
    ),
    SeedTicket(
        key="OPS-16",
        summary="Load test the checkout flow before the autumn sale",
        issue_type="Task",
        created_days_ago=6,
        description="Run the k6 checkout scenario at 3x last year's peak.",
        labels=("order-service", "performance"),
        components=("orders",),
        **_todo(),
    ),
    SeedTicket(
        key="WEB-1",
        summary="payment page timeout on mobile Safari",
        issue_type="Bug",
        created_days_ago=2,
        description="Frontend project; not visible through the OPS-scoped tickets MCP.",
        labels=("payment-service",),
        **_todo(),
    ),
)


def rows(now: datetime) -> list[dict[str, Any]]:
    """Seed tickets as DB rows anchored at ``now``."""
    out: list[dict[str, Any]] = []
    for t in TICKETS:
        created = now - timedelta(days=t.created_days_ago)
        resolved = now - timedelta(days=t.resolved_days_ago) if t.resolved_days_ago else None
        comment_times = [now - timedelta(days=days) for _, _, days in t.comments]
        comments = [
            {"author": author, "body": body, "created": ts}
            for (author, body, _), ts in zip(t.comments, comment_times, strict=True)
        ]
        updated = max([created, *comment_times, *([resolved] if resolved else [])])
        out.append(
            {
                "key": t.key,
                "project": t.project,
                "number": t.number,
                "summary": t.summary,
                "description": t.description,
                "issue_type": t.issue_type,
                "status": t.status,
                "status_category": t.status_category,
                "priority": t.priority,
                "resolution": t.resolution,
                "labels": list(t.labels),
                "components": list(t.components),
                "reporter": t.reporter,
                "assignee": t.assignee,
                "created": created,
                "updated": updated,
                "resolved": resolved,
                "comments": comments,
            }
        )
    return out


class TicketSeedError(Exception):
    pass


_ISSUE_COLUMNS = (
    "key",
    "project",
    "number",
    "summary",
    "description",
    "issue_type",
    "status",
    "status_category",
    "priority",
    "resolution",
    "labels",
    "components",
    "reporter",
    "assignee",
    "created",
    "updated",
    "resolved",
)


class PostgresTicketSeeder:
    def __init__(self, dsn: str, schema: str = "tickets") -> None:
        self.dsn = dsn
        self.schema = schema

    def seed(self, now: datetime) -> dict[str, int]:
        """Replace the seeded projects' tickets. Returns ticket counts per status category."""
        data = rows(now)
        schema = sql.Identifier(self.schema)
        insert_issue = sql.SQL("INSERT INTO {}.issues ({}) VALUES ({})").format(
            schema,
            sql.SQL(", ").join(map(sql.Identifier, _ISSUE_COLUMNS)),
            sql.SQL(", ").join(sql.Placeholder() * len(_ISSUE_COLUMNS)),
        )
        insert_comment = sql.SQL(
            "INSERT INTO {}.comments (issue_key, body, author, created) VALUES (%s, %s, %s, %s)"
        ).format(schema)
        try:
            with psycopg.connect(self.dsn, connect_timeout=5) as conn:
                found = conn.execute(
                    "SELECT to_regclass(%s) IS NOT NULL", [f"{self.schema}.issues"]
                ).fetchone()
                if not found or not found[0]:
                    raise TicketSeedError(
                        f"Table {self.schema}.issues does not exist. mock-tickets-mcp creates it on "
                        "start: run `make mock-tickets-up` first."
                    )
                with conn.transaction():
                    conn.execute(
                        sql.SQL("DELETE FROM {}.issues WHERE project = ANY(%s)").format(schema),
                        [list(SEEDED_PROJECTS)],
                    )
                    for row in data:
                        conn.execute(insert_issue, [row[c] for c in _ISSUE_COLUMNS])
                        for comment in row["comments"]:
                            conn.execute(
                                insert_comment,
                                [
                                    row["key"],
                                    comment["body"],
                                    comment["author"],
                                    comment["created"],
                                ],
                            )
        except psycopg.Error as exc:
            raise TicketSeedError(f"Postgres error: {exc}. Is `make infra-up` running?") from exc
        counts: dict[str, int] = {}
        for row in data:
            counts[row["status_category"]] = counts.get(row["status_category"], 0) + 1
        return counts


def default_dsn() -> str:
    """DSN from the same POSTGRES_* variables docker-compose.infra.yml uses."""
    user = os.environ.get("POSTGRES_USER") or "aiops"
    password = os.environ.get("POSTGRES_PASSWORD") or "aiops-local-only"
    database = os.environ.get("POSTGRES_DB") or "aiops"
    port = os.environ.get("POSTGRES_PORT") or "15432"
    host = os.environ.get("POSTGRES_HOST") or "localhost"
    return f"postgresql://{quote(user)}:{quote(password, safe='')}@{host}:{port}/{database}"
