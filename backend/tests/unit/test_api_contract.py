"""Contract conformance (docs/api/contract.md): every response of a real replay run and of the
demo history validates against the JSON Schemas in docs/schemas/ and against the pydantic
models mirroring the contract (aiops.api.models)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from pydantic import BaseModel, TypeAdapter

from aiops.api import models as m
from aiops.core.config import load_settings
from aiops.core.events import INVESTIGATION_EVENT_TYPES, InvestigationEvent
from aiops.orchestrator.demo import build_demo, seed_demo
from aiops.store.repository import InvestigationStore
from tests.api_support import api_client, make_context, parse_sse, wait_until_idle
from tests.conftest import REPO_ROOT

SCHEMAS = REPO_ROOT / "docs" / "schemas"
#: event type -> (data key, JSON Schema of each item, is a list)
EVENT_PAYLOADS = {
    "plan_created": [("steps", "InvestigationStep", True), ("context", "IncidentContext", False)],
    "evidence_added": [("evidence", "Evidence", False)],
    "hypothesis_ranked": [("hypotheses", "Hypothesis", True)],
    "report_ready": [("report", "InvestigationReport", False)],
}
#: The data keys the contract table lists per event type.
EVENT_KEYS = {
    "investigation_started": {"question"},
    "clarification_needed": {"question", "candidates"},
    "plan_created": {"context", "steps"},
    "round_started": {"round", "agents"},
    "agent_started": {"step_id", "objective", "round"},
    "tool_called": {"step_id", "tool", "status", "duration_ms"},
    "evidence_added": {"step_id", "evidence"},
    "agent_finished": {
        "step_id",
        "status",
        "summary",
        "signals",
        "evidence_count",
        "duration_ms",
        "tokens",
    },
    "rca_started": set(),
    "hypothesis_ranked": {"hypotheses"},
    "report_ready": {"report"},
    "approval_requested": {"approval_id", "action"},
    "investigation_finished": {"status", "duration_ms"},
    "error": {"message", "recoverable"},
    "heartbeat": set(),
}


def json_schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    return loaded


def conforms(model: type[BaseModel] | Any, payload: Any) -> None:
    TypeAdapter(model).validate_python(payload)


@pytest.fixture(scope="module")
def responses(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    """Every endpoint's response over the demo history + one live-served replay."""
    import asyncio

    async def collect() -> dict[str, Any]:
        settings = load_settings("local", REPO_ROOT / "config")
        tmp: Path = tmp_path_factory.mktemp("contract")
        store = InvestigationStore(f"sqlite:///{tmp / 'c.db'}")
        store.migrate()
        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        await seed_demo(store, await build_demo(settings, now))
        ctx = make_context(settings, store)
        out: dict[str, Any] = {}
        async with api_client(ctx) as c:
            out["health"] = (await c.get("/api/health")).json()
            out["services"] = (await c.get("/api/services")).json()
            out["agents"] = (await c.get("/api/agents")).json()
            out["dashboard"] = (await c.get("/api/dashboard/summary")).json()
            out["page"] = (await c.get("/api/investigations?limit=200")).json()
            ids = [i["id"] for i in out["page"]["items"]]
            out["investigations"] = [(await c.get(f"/api/investigations/{i}")).json() for i in ids]
            created = await c.post("/api/investigations", json={"question": "x", "scenario": "S3"})
            out["created"] = created.json()
            inv_id = out["created"]["id"]
            frames = parse_sse((await c.get(f"/api/investigations/{inv_id}/events")).text)
            await wait_until_idle(ctx)
            out["investigations"].append((await c.get(f"/api/investigations/{inv_id}")).json())
            stored = parse_sse((await c.get(f"/api/investigations/{ids[0]}/events")).text)
            out["events"] = [f["data"] for f in frames + stored]
            draft = (await c.post(f"/api/investigations/{inv_id}/tickets/draft")).json()
            out["draft"] = draft
            out["approved"] = (
                await c.post(f"/api/approvals/{draft['approval_id']}/approve", json={"by": "t"})
            ).json()
            out["approvals"] = (await c.get("/api/approvals")).json()
            out["scenarios"] = (await c.get("/api/scenarios")).json()
            out["errors"] = [
                (await c.get("/api/investigations/nope")).json(),
                (await c.post("/api/investigations", json={})).json(),
                (await c.post("/api/scenarios/S1/inject")).json(),
            ]
        return out

    return asyncio.run(collect())


def test_investigations_match_the_json_schema(responses: dict[str, Any]) -> None:
    schema = json_schema("Investigation")
    assert len(responses["investigations"]) == 47
    for inv in responses["investigations"]:
        jsonschema.validate(inv, schema)


def test_lists_and_dashboard_match_the_contract_models(responses: dict[str, Any]) -> None:
    conforms(m.HealthResponse, responses["health"])
    conforms(list[m.ServiceOut], responses["services"])
    conforms(list[m.AgentOut], responses["agents"])
    conforms(m.DashboardSummary, responses["dashboard"])
    conforms(m.InvestigationPage, responses["page"])
    conforms(m.CreatedInvestigation, responses["created"])
    conforms(m.TicketDraftResponse, responses["draft"])
    conforms(m.ApprovalOut, responses["approved"])
    conforms(list[m.ApprovalOut], responses["approvals"])
    conforms(list[m.ScenarioOut], responses["scenarios"])
    for error in responses["errors"]:
        conforms(m.ErrorResponse, error)
    summary_keys = {
        "id",
        "incident",
        "status",
        "report",
        "affected_services",
        "created_at",
        "completed_at",
        "duration_ms",
        "mode",
    }
    assert all(set(i) == summary_keys for i in responses["page"]["items"])
    assert set(responses["dashboard"]) == {
        "window_days",
        "totals",
        "mttr_minutes",
        "avg_confidence",
        "by_day",
        "by_service",
        "top_signals",
        "agents",
        "recent",
    }
    incident = json_schema("Incident")
    for item in responses["page"]["items"]:
        jsonschema.validate(item["incident"], incident)
    assert {"id", "status"} <= set(responses["created"])


def test_events_follow_the_envelope_and_the_per_type_data(responses: dict[str, Any]) -> None:
    events = responses["events"]
    assert {e["type"] for e in events} >= {
        "investigation_started",
        "plan_created",
        "round_started",
        "agent_started",
        "tool_called",
        "evidence_added",
        "agent_finished",
        "rca_started",
        "hypothesis_ranked",
        "report_ready",
        "investigation_finished",
        "error",
    }
    for event in events:
        InvestigationEvent.model_validate(event)
        assert set(event) == {"type", "investigation_id", "timestamp", "seq", "agent", "data"}
        assert event["type"] in INVESTIGATION_EVENT_TYPES
        assert EVENT_KEYS[event["type"]] <= set(event["data"]), event
        for key, name, many in EVENT_PAYLOADS.get(event["type"], []):
            value = event["data"][key]
            for item in value if many else [value]:
                jsonschema.validate(item, json_schema(name))
