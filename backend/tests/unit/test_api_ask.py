"""POST /api/ask (PR-041): platform questions answered from live data without starting an
investigation; incident questions start one like POST /investigations. Replays use the
recorded fixtures (zero tokens)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from aiops.agents.registry import AGENTS
from aiops.api import models as m
from aiops.api.context import ApiContext
from aiops.api.health import CapabilityHealth
from aiops.core.config import CapabilityConfig, Settings, load_settings
from aiops.store.repository import InvestigationStore
from tests.api_support import api_client, make_context, wait_until_idle
from tests.conftest import REPO_ROOT
from tests.unit.test_api import with_llm_key


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", REPO_ROOT / "config")


@pytest.fixture
def store(tmp_path: Path) -> InvestigationStore:
    store = InvestigationStore(f"sqlite:///{tmp_path / 'ask.db'}")
    store.migrate()
    return store


@pytest.fixture
def ctx(settings: Settings, store: InvestigationStore) -> ApiContext:
    return make_context(settings, store)


def parsed(body: dict[str, Any]) -> m.AskResponse:
    return TypeAdapter(m.AskResponse).validate_python(body)


async def ask(client: Any, question: str) -> dict[str, Any]:
    response = await client.post("/api/ask", json={"question": question})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    parsed(body)
    return body


async def test_platform_questions_never_start_an_investigation(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        catalog = await ask(client, "which agents do you have?")
        health = await ask(client, "is the LLM configured?")
        idle = await ask(client, "what are the agents that are running now?")
        help_ = await ask(client, "what should I ask?")
        recent = await ask(client, "recent incidents on order-service")
    assert await ctx.store.count() == 0
    assert catalog["kind"] == "platform" and catalog["intent"] == "agents_catalog"
    agents = catalog["answer"]["items"]
    assert {a["name"] for a in agents} == {s.name for s in AGENTS.specs()}
    assert len(agents) == 7
    metrics = next(a for a in agents if a["name"] == "metrics")
    assert metrics["providers"] == ["prometheus"] and metrics["type"] == "agent"
    llm = next(c for c in health["answer"]["items"] if c["name"] == "LLM")
    assert llm["status"] == "down" and "OPENAI_COMPAT_API_KEY" in llm["detail"]
    assert "replay mode" in health["answer"]["markdown"]
    assert idle["intent"] == "agents_running"
    assert idle["answer"]["title"] == "Nothing is running right now"
    scenarios = [i["scenario"] for i in help_["answer"]["items"] if i.get("scenario")]
    assert "S1" in scenarios  # replay mode: the recorded questions are the examples
    assert recent["intent"] == "recent_investigations"
    assert recent["answer"]["items"] == []


async def test_running_investigations_list_agents_and_tools(
    ctx: ApiContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOPS_REPLAY_TOOL_DELAY_S", "0.05")
    async with api_client(ctx) as client:
        created = await ask(client, "Payment API is returning HTTP 500 in production")
        inv_id = created["investigation_id"]
        running: dict[str, Any] = {}
        async with asyncio.timeout(15):
            while True:
                answer = await ask(client, "what agents are running now?")
                items = answer["answer"]["items"]
                if items and items[0]["type"] == "running_investigation":
                    running = items[0]
                    if any(a["tool"] for a in running["agents"]):
                        break
                await asyncio.sleep(0.05)
        await client.post(f"/api/investigations/{inv_id}/cancel")
        await wait_until_idle(ctx)
        after = await ask(client, "what's running?")
        recent = await ask(client, "what happened with the last payment incident?")
    assert created["kind"] == "incident" and created["mode"] == "replay"
    assert created["scenario"] == "S1" and created["answer"] is None
    assert running["id"] == inv_id and running["href"] == f"/investigations/{inv_id}"
    assert running["service"] == "payment-service" and running["round"] == 1
    assert {a["agent"] for a in running["agents"]} >= {"logs", "metrics"}
    assert running["elapsed_s"] >= 0
    assert answer["answer"]["links"][0]["href"] == f"/investigations/{inv_id}"
    assert after["answer"]["title"] == "Nothing is running right now"
    assert [i["id"] for i in after["answer"]["items"]] == [inv_id]
    assert recent["answer"]["items"][0]["id"] == inv_id


async def test_unknown_incident_answers_with_the_scenarios(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        body = await ask(client, "hello world")
    assert body["kind"] == "incident" and body["investigation_id"] is None
    assert body["source"] == "default"
    chips = body["answer"]["items"]
    assert {c["scenario"] for c in chips} >= {"S1", "S2", "S3", "S4", "S5"}
    assert all(c["type"] == "suggestion" and c["question"] for c in chips)
    assert "no LLM is configured" in body["answer"]["markdown"]
    assert await ctx.store.count() == 0


async def down_probe(cap: CapabilityConfig, timeout: float) -> bool:
    return cap.provider not in {"prometheus", "elasticsearch"}


async def test_replay_error_says_exactly_why(settings: Settings, store: InvestigationStore) -> None:
    """The LLM IS configured, two capabilities are down: the message names only those."""
    ctx = make_context(with_llm_key(settings), store)
    ctx.health = CapabilityHealth(ctx.settings, probe=down_probe)
    async with api_client(ctx) as client:
        response = await client.post("/api/investigations", json={"question": "hello world"})
        live = await client.post(
            "/api/investigations", json={"question": "hello world", "mode": "live"}
        )
        health = await ask(client, "what's down?")
    message = response.json()["error"]["message"]
    assert response.status_code == 422
    assert "capabilities are unreachable: logs, metrics" in message
    assert "LLM" not in message and "No LLM configured or" not in message
    assert "unreachable: logs, metrics" in live.json()["error"]["message"]
    down = {c["name"] for c in health["answer"]["items"] if c["status"] == "down"}
    assert down == {"logs", "metrics"}


async def test_no_llm_error_names_the_missing_setting(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        response = await client.post("/api/investigations", json={"question": "hello world"})
    message = response.json()["error"]["message"]
    assert "no LLM is configured (" in message and "OPENAI_COMPAT_API_KEY" in message
    assert "unreachable" not in message


async def test_ask_validates_the_question(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        response = await client.post("/api/ask", json={"question": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
