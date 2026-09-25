"""A small OPS backlog for tests (the full seed lives in `aiops seed tickets`)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from mock_tickets_mcp.models import Comment, Issue

NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


def issues() -> list[Issue]:
    return [
        Issue(
            key="OPS-2",
            summary="payment-service connection pool exhausted during peak traffic",
            description="Connection timeouts on checkout; pool raised to 20.",
            issue_type="Incident",
            status="Done",
            status_category="Done",
            resolution="Fixed",
            priority="High",
            labels=["payment-service", "database"],
            components=["payments"],
            created=ago(200),
            updated=ago(199),
            resolved=ago(199),
        ),
        Issue(
            key="OPS-3",
            summary="order-service OOMKilled after bulk export release",
            description="Pods OOMKilled repeatedly; HTTP 503 during restarts.",
            issue_type="Incident",
            status="Done",
            status_category="Done",
            resolution="Fixed",
            labels=["order-service", "oom"],
            components=["orders"],
            created=ago(45),
            updated=ago(44),
            resolved=ago(44),
        ),
        Issue(
            key="OPS-7",
            summary="inventory-service slow query on stock reservation",
            description="Sequential scan; order-service reservation calls hit timeouts.",
            issue_type="Bug",
            status="In Progress",
            status_category="In Progress",
            priority="High",
            labels=["inventory-service", "slow-query"],
            components=["inventory"],
            created=ago(8),
            updated=ago(1),
            comments=[
                Comment(id="1", body="Index migration drafted.", author="dana", created=ago(1))
            ],
        ),
        Issue(
            key="OPS-12",
            summary="payment-service DB connection timeouts",
            description="Requests fail with HTTP 500 when the pool is exhausted.",
            issue_type="Bug",
            status="Open",
            status_category="To Do",
            priority="High",
            labels=["payment-service", "database", "known-issue"],
            components=["payments"],
            reporter="sam",
            created=ago(3),
            updated=ago(2),
        ),
        Issue(
            key="WEB-1",
            summary="payment page timeout on mobile",
            issue_type="Bug",
            status="Open",
            status_category="To Do",
            labels=["payment-service"],
            created=ago(2),
            updated=ago(2),
        ),
    ]
