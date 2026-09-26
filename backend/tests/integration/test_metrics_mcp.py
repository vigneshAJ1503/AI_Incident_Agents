"""Requires Prometheus + prometheus-mcp (`make infra-up k8s-up prometheus-mcp-up`).
Run with: make test-integration"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aiops.core.config import load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.mcp.registry import MCPRegistry

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "config"


async def test_metrics_capability_end_to_end() -> None:
    settings = load_settings("local", CONFIG)
    audit = MemoryAuditSink()
    now = datetime.now(UTC).replace(microsecond=0)
    async with MCPRegistry(settings, audit=audit).toolset("metrics", agent="it") as tools:
        names = {s.name for s in await tools.specs()}
        assert names == {"query", "query_range", "list_metrics", "metric_metadata", "get_targets"}

        targets = await tools.call("get_targets", {})
        assert targets.ok, targets.content
        jobs = {t["job"] for t in targets.data["targets"]}
        assert {"sample-services", "kube-state-metrics"} <= jobs

        listed = await tools.call("list_metrics", {"match": "http_request"})
        assert listed.ok and "http_requests_total" in listed.data["metrics"]

        ranged = await tools.call(
            "query_range",
            {
                "query": 'sum by (service) (rate(http_requests_total{namespace="prod"}[2m]))',
                "start": (now - timedelta(minutes=15)).isoformat(),
                "end": now.isoformat(),
                "step": "30s",
            },
        )
        assert ranged.ok, ranged.content
        services = {s["labels"]["service"] for s in ranged.data["series"]}
        assert "payment-service" in services
        assert ranged.data["step_s"] == 30
        assert all(len(s["values"]) <= 31 for s in ranged.data["series"])

        # server-side guardrails: an oversized range and a whole-TSDB selector
        too_long = await tools.call(
            "query_range",
            {
                "query": "up",
                "start": (now - timedelta(days=30)).isoformat(),
                "end": now.isoformat(),
            },
        )
        assert not too_long.ok and "exceeds the maximum" in too_long.content
        scan = await tools.call("query", {"query": '{__name__=~".+"}'})
        assert not scan.ok and "metric name" in scan.content
    assert [r.tool_call.status for r in audit.records][-2:] == ["error", "error"]
