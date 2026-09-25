"""Requires `make infra-up mcp-up seed-logs S=S1`. Run with: make test-integration"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiops.core.config import load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.mcp.registry import MCPRegistry

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "config"


async def test_logs_capability_end_to_end() -> None:
    settings = load_settings("local", CONFIG)
    audit = MemoryAuditSink()
    end = datetime.now(UTC)
    start = end - timedelta(minutes=30)
    async with MCPRegistry(settings, audit=audit).toolset("logs", agent="it") as tools:
        names = {s.name for s in await tools.specs()}
        assert names == {"list_indices", "get_mapping", "search_logs", "execute_esql"}
        outcome = await tools.call(
            "execute_esql",
            {
                "query": 'FROM payment-prod-* | WHERE level == "ERROR" | STATS n = COUNT(*) BY error_type',
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        assert outcome.ok, outcome.content
        assert outcome.data["columns"] == ["n", "error_type"]
        guarded = await tools.call(
            "execute_esql",
            {
                "query": "FROM .security | LIMIT 1",
                "start": start.isoformat(),
                "end": end.isoformat(),
            },
        )
        assert not guarded.ok and "not allowed" in guarded.content
    assert [r.tool_call.status for r in audit.records] == ["ok", "error"]
