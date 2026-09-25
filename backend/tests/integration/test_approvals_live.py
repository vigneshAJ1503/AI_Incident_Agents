"""Approved ticket write against the live mock-tickets-mcp (make infra-up mock-tickets-up).

Creates a real ticket in schema `tickets`, then reseeds the backlog at FIXED_NOW.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from aiops.core.config import load_settings
from aiops.core.guardrails.approvals import (
    ActionStatus,
    ApprovalExecutor,
    ApprovalPolicy,
    ApprovalService,
    MemoryApprovalAuditSink,
    MemoryApprovalStore,
)
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.mcp import tickets
from aiops.mcp.registry import MCPRegistry
from aiops.seed.tickets import PostgresTicketSeeder, default_dsn
from tests.conftest import REPO_ROOT
from tests.fixtures.scenario_context import FIXED_NOW

pytestmark = pytest.mark.integration


async def test_approved_create_and_comment_reach_the_mock() -> None:
    settings = load_settings("local", REPO_ROOT / "config")
    service = ApprovalService(
        MemoryApprovalStore(),
        MemoryApprovalAuditSink(),
        ApprovalPolicy(settings),
        ttl=timedelta(hours=1),
    )
    registry = MCPRegistry(settings, audit=MemoryAuditSink())
    executor = ApprovalExecutor(service, registry)
    try:
        create = service.propose(
            action="create_ticket",
            capability="tickets",
            tool=tickets.CREATE_ISSUE,
            arguments={
                "project_key": "OPS",
                "summary": "[payment-service] integration test ticket",
                "issue_type": "Bug",
                "description": "created by test_approvals_live",
                "components": "payments",
                "additional_fields": '{"labels": ["aiops"]}',
            },
            reason="integration test",
            requested_by="pytest",
        )
        service.approve(create.id, "alice")
        done = await executor.execute(create.id, "alice")
        assert done.status is ActionStatus.EXECUTED, done.error
        key = done.result["issue"]["key"] if done.result else ""
        assert key.startswith("OPS-")

        comment = service.propose(
            action="comment_ticket",
            capability="tickets",
            tool=tickets.ADD_COMMENT,
            arguments={"issue_key": key, "body": "investigation inv-test"},
            reason="integration test",
            requested_by="pytest",
        )
        service.approve(comment.id, "alice")
        assert (await executor.execute(comment.id, "alice")).status is ActionStatus.EXECUTED

        async with registry.toolset("tickets", agent="pytest") as read:
            outcome = await read.call(
                tickets.GET_ISSUE,
                {"issue_key": key, "fields": tickets.TICKET_FIELDS, "include": "comments"},
            )
        issue = tickets.as_payload(outcome.data, outcome.text)
        assert issue["labels"] == ["aiops"] and issue["components"] == ["payments"]
        assert issue["comments"][0]["body"] == "investigation inv-test"
    finally:
        PostgresTicketSeeder(default_dsn()).seed(FIXED_NOW)
