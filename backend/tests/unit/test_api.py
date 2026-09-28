"""REST + SSE API (PR-035) on an httpx AsyncClient: SQLite store, in-memory approvals, fake
fault controller and executor. Replays use the recorded fixtures (zero tokens)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import jsonschema
import pytest
from pydantic import SecretStr

from aiops.api.context import ApiContext
from aiops.api.health import CapabilityHealth, llm_configured
from aiops.api.runner import InvestigationRunner
from aiops.api.scenarios import ScenarioCatalog
from aiops.api.sse import event_stream
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import ApiConfig, CapabilityConfig, LLMConfig, Settings, load_settings
from aiops.core.events import EventBus, InvestigationEvent
from aiops.core.models import (
    Incident,
    Investigation,
    InvestigationReport,
    InvestigationStatus,
)
from aiops.orchestrator.dashboard import dashboard_summary
from aiops.store.repository import InvestigationStore
from tests.api_support import (
    FakeFaults,
    api_client,
    make_context,
    parse_sse,
    wait_until_idle,
)
from tests.conftest import REPO_ROOT

CONFIG = REPO_ROOT / "config"
SCHEMAS = REPO_ROOT / "docs" / "schemas"
#: Fake secrets built at runtime (the repo is public; gitleaks runs in CI).
FAKE_LLM_KEY = "-".join(["fake", "llm", uuid4().hex])
FAKE_API_KEY = uuid4().hex


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


@pytest.fixture
def store(tmp_path: Path) -> InvestigationStore:
    store = InvestigationStore(f"sqlite:///{tmp_path / 'api.db'}")
    store.migrate()
    return store


@pytest.fixture
def ctx(settings: Settings, store: InvestigationStore) -> ApiContext:
    return make_context(settings, store)


def schema(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((SCHEMAS / f"{name}.schema.json").read_text())
    return loaded


def with_llm_key(settings: Settings) -> Settings:
    llm = LLMConfig(
        provider="openai_compat",
        base_url="https://llm.example.com/v1",
        api_key=SecretStr(FAKE_LLM_KEY),
        models={"agent": "some-model"},
    )
    return settings.model_copy(update={"llm": llm})


def stored(inv_id: str, created: datetime, **update: Any) -> Investigation:
    report = InvestigationReport(summary=f"summary {inv_id}", severity="high", confidence=0.8)
    base = Investigation(
        id=inv_id,
        incident=Incident(title=f"Incident {inv_id}", service="payment-service"),
        status=InvestigationStatus.COMPLETED,
        report=report,
        created_at=created,
        completed_at=created + timedelta(minutes=2),
        duration_ms=120_000,
        mode="demo",
    )
    return base.model_copy(update=update)


async def run_replay(client: Any, **body: Any) -> tuple[str, list[dict[str, Any]]]:
    created = await client.post("/api/investigations", json=body)
    assert created.status_code == 202, created.text
    inv_id = created.json()["id"]
    stream = await client.get(f"/api/investigations/{inv_id}/events")
    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    return inv_id, parse_sse(stream.text)


# --------------------------------------------------------------------------- health, catalog


async def test_health_reports_profile_llm_and_capabilities_without_secrets(
    settings: Settings, store: InvestigationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOPS_ENABLE_FAULTS", "1")
    ctx = make_context(with_llm_key(settings), store)
    async with api_client(ctx) as client:
        body = (await client.get("/api/health")).json()
    assert body["status"] == "ok" and body["store"] == "ok"
    assert body["profile"] == "local"
    assert body["llm"] == {"provider": "openai_compat", "configured": True}
    assert set(body["capabilities"]) == set(settings.capabilities)
    assert set(body["capabilities"].values()) == {"ok"}
    assert body["faults_enabled"] is True
    assert FAKE_LLM_KEY not in json.dumps(body)


@pytest.mark.parametrize(
    ("llm", "configured"),
    [
        ({"provider": "anthropic", "api_key": FAKE_LLM_KEY, "models": {"agent": "m"}}, True),
        ({"provider": "anthropic", "models": {"agent": "m"}}, False),
        ({"provider": "bedrock", "models": {"agent": "m"}}, True),  # AWS chain, no key
        (
            {
                "provider": "azure_openai",
                "base_url": "https://acme.openai.azure.com",
                "api_key": FAKE_LLM_KEY,
                "api_version": "2024-10-21",
                "models": {"agent": "gpt-deployment"},
            },
            True,
        ),
        ({"provider": "azure_openai", "api_key": FAKE_LLM_KEY, "models": {"agent": "d"}}, False),
    ],
)
async def test_health_reports_each_enterprise_llm_provider(
    settings: Settings,
    store: InvestigationStore,
    monkeypatch: pytest.MonkeyPatch,
    llm: dict[str, Any],
    configured: bool,
) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    profile = settings.model_copy(update={"llm": LLMConfig.model_validate(llm)})
    async with api_client(make_context(profile, store)) as client:
        body = (await client.get("/api/health")).json()
    assert body["llm"] == {"provider": llm["provider"], "configured": configured}
    assert FAKE_LLM_KEY not in json.dumps(body)


def test_llm_configured_needs_key_and_model(settings: Settings) -> None:
    assert not llm_configured(settings)  # no key on this machine / in CI
    assert llm_configured(with_llm_key(settings))


async def test_capability_health_is_cached_and_marks_disabled(settings: Settings) -> None:
    calls: list[str] = []

    async def probe(cap: CapabilityConfig, timeout: float) -> bool:
        calls.append(cap.provider)
        return cap.provider != "prometheus"

    caps = dict(settings.capabilities)
    caps["code"] = caps["code"].model_copy(update={"enabled": False})
    tuned = settings.model_copy(update={"capabilities": caps})
    now = [0.0]
    health = CapabilityHealth(tuned, probe=probe, ttl_s=30, clock=lambda: now[0])
    first = await health.status()
    assert first["code"] == "disabled" and first["metrics"] == "down" and first["logs"] == "ok"
    assert not await health.all_reachable()
    count = len(calls)
    await health.status()
    assert len(calls) == count  # cached
    now[0] = 31
    await health.status()
    assert len(calls) == 2 * count


async def test_services_and_agents(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        services = (await client.get("/api/services")).json()
        agents = (await client.get("/api/agents")).json()
    names = {s["name"] for s in services}
    assert {"payment-service", "order-service"} <= names
    payment = next(s for s in services if s["name"] == "payment-service")
    assert payment["environments"] and isinstance(payment["owners"], dict)
    assert {a["name"] for a in agents} >= {"logs", "metrics", "alerts", "k8s", "code"}
    assert all(a["last_run_at"] is None and a["runs_7d"] == 0 for a in agents)


# --------------------------------------------------------------------------- investigations


async def test_replay_investigation_streams_to_completion(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        created = await client.post(
            "/api/investigations", json={"question": "Payment API is returning HTTP 500"}
        )
        assert created.status_code == 202
        body = created.json()
        assert body["status"] == "pending" and body["mode"] == "replay"
        assert body["scenario"] == "S1"
        stream = await client.get(f"/api/investigations/{body['id']}/events")
        frames = parse_sse(stream.text)
        await wait_until_idle(ctx)
        inv = (await client.get(f"/api/investigations/{body['id']}")).json()
        markdown = await client.get(f"/api/investigations/{body['id']}/report.md")
        # a reconnect replays only what's after Last-Event-ID (header or query)
        last = len(frames)
        tail = parse_sse(
            (
                await client.get(
                    f"/api/investigations/{body['id']}/events",
                    headers={"Last-Event-ID": str(last - 2)},
                )
            ).text
        )
        tail_q = parse_sse(
            (
                await client.get(
                    f"/api/investigations/{body['id']}/events?last_event_id={last - 1}"
                )
            ).text
        )
    seqs = [f["data"]["seq"] for f in frames]
    assert seqs == list(range(1, len(frames) + 1))
    assert [f["id"] for f in frames] == [str(s) for s in seqs]
    assert all(f["event"] == f["data"]["type"] for f in frames)
    assert frames[0]["event"] == "investigation_started"
    assert frames[-1]["event"] == "investigation_finished"
    assert frames[-1]["data"]["data"]["status"] == "completed"
    for frame in frames:  # every event matches the contract envelope
        InvestigationEvent.model_validate(frame["data"])
    assert {"plan_created", "agent_finished", "report_ready"} <= {f["event"] for f in frames}
    assert inv["status"] == "completed" and inv["mode"] == "replay"
    assert inv["report"]["confidence"] >= 0.7
    assert "pool" in inv["hypotheses"][0]["statement"].lower()
    jsonschema.validate(inv, schema("Investigation"))
    assert markdown.status_code == 200
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert markdown.text.startswith("# Incident report")
    assert [f["data"]["seq"] for f in tail] == [last - 1, last]
    assert [f["data"]["seq"] for f in tail_q] == [last]


async def test_named_scenario_and_refusals(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        s0 = await client.post(
            "/api/investigations", json={"question": "anything wrong?", "scenario": "s0"}
        )
        unknown = await client.post("/api/investigations", json={"question": "x", "scenario": "S9"})
        no_match = await client.post("/api/investigations", json={"question": "hello world"})
        live = await client.post(
            "/api/investigations",
            json={"question": "Payment API is returning HTTP 500", "mode": "live"},
        )
        invalid = await client.post("/api/investigations", json={"question": ""})
        await wait_until_idle(ctx)
        healthy = (await client.get(f"/api/investigations/{s0.json()['id']}")).json()
    assert s0.status_code == 202 and s0.json()["scenario"] == "S0"
    assert healthy["report"]["severity"] == "none"
    assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "unknown_scenario"
    assert no_match.status_code == 422
    assert no_match.json()["error"]["code"] == "no_matching_scenario"
    assert "S1" in no_match.json()["error"]["message"]
    assert live.status_code == 409 and live.json()["error"]["code"] == "live_unavailable"
    assert "OPENAI_COMPAT_API_KEY" in live.json()["error"]["message"]
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"


async def test_concurrency_limit(ctx: ApiContext) -> None:
    ctx.runner.max_running = 0
    async with api_client(ctx) as client:
        response = await client.post(
            "/api/investigations", json={"question": "x", "scenario": "S1"}
        )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "too_many_investigations"


async def test_not_found_and_report_not_ready(ctx: ApiContext) -> None:
    await ctx.store.save(stored("inv-noreport", datetime.now(UTC), report=None))
    async with api_client(ctx) as client:
        missing = await client.get("/api/investigations/inv-missing")
        events = await client.get("/api/investigations/inv-missing/events")
        report = await client.get("/api/investigations/inv-noreport/report.md")
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {"code": "not_found", "message": "Investigation 'inv-missing' not found"}
    }
    assert events.status_code == 404
    assert report.status_code == 409 and report.json()["error"]["code"] == "report_not_ready"


async def test_list_filters_and_cursor_pagination(ctx: ApiContext) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    items = [stored(f"inv-{i:02d}", now - timedelta(hours=i // 2)) for i in range(9)]  # ties
    items.append(stored("inv-failed", now - timedelta(days=1), status=InvestigationStatus.FAILED))
    items.append(
        stored(
            "inv-order",
            now - timedelta(days=2),
            incident=Incident(title="Orders time out", service="order-service"),
            report=InvestigationReport(summary="slow inventory", severity="critical"),
        )
    )
    for inv in items:
        await ctx.store.save(inv)
    async with api_client(ctx) as client:
        seen: list[str] = []
        cursor = None
        pages = 0
        while True:
            params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
            page = (await client.get("/api/investigations", params=params)).json()
            seen += [i["id"] for i in page["items"]]
            pages += 1
            cursor = page["next_cursor"]
            if cursor is None:
                break
        failed = (await client.get("/api/investigations?status=failed")).json()
        order = (await client.get("/api/investigations?service=orders")).json()
        critical = (await client.get("/api/investigations?severity=critical")).json()
        text = (await client.get("/api/investigations?q=INVENTORY")).json()
        bad_status = await client.get("/api/investigations?status=bogus")
        bad_cursor = await client.get("/api/investigations?cursor=%%%")
    assert pages == 4 and len(seen) == len(set(seen)) == len(items)
    expected = sorted(items, key=lambda i: (i.created_at, i.id), reverse=True)
    assert seen == [i.id for i in expected]
    assert [i["id"] for i in failed["items"]] == ["inv-failed"]
    assert [i["id"] for i in order["items"]] == ["inv-order"]  # alias resolved via catalog
    assert [i["id"] for i in critical["items"]] == ["inv-order"]
    assert [i["id"] for i in text["items"]] == ["inv-order"]
    assert bad_status.status_code == 422
    assert bad_cursor.status_code == 400 and bad_cursor.json()["error"]["code"] == "invalid_cursor"


async def test_dashboard_matches_the_shared_computation(ctx: ApiContext) -> None:
    now = datetime.now(UTC)
    for i in range(6):
        await ctx.store.save(stored(f"inv-d{i}", now - timedelta(days=i * 3, hours=1)))
    async with api_client(ctx) as client:
        body = (await client.get("/api/dashboard/summary?days=14")).json()
        bad = await client.get("/api/dashboard/summary?days=0")
    invs = [inv for i in range(6) if (inv := await ctx.store.get(f"inv-d{i}"))]
    expected = dashboard_summary(invs, days=14, now=now)
    assert body["window_days"] == 14
    assert body["totals"] == expected["totals"] and body["totals"]["investigations"] == 5
    assert body["mttr_minutes"] == {"p50": 2.0, "p90": 2.0}
    assert len(body["by_day"]) == 14 and len(body["recent"]) == 5
    assert bad.status_code == 422


async def test_cancel_a_running_investigation(
    settings: Settings, store: InvestigationStore
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class SlowOrchestrator:
        def __init__(self, bus: EventBus) -> None:
            self.bus = bus
            self.cancelled = False

        def running(self, investigation_id: str) -> Investigation | None:
            return None

        def cancel(self, investigation_id: str) -> bool:
            self.cancelled = True
            release.set()
            return True

        async def investigate(self, request: Any, *, investigation_id: str) -> Investigation:
            self.bus.publish("investigation_started", investigation_id, question=request.question)
            started.set()
            await release.wait()
            status = "cancelled" if self.cancelled else "completed"
            self.bus.publish("investigation_finished", investigation_id, status=status)
            return stored(
                investigation_id,
                datetime.now(UTC),
                status=InvestigationStatus(status),
                report=None,
            )

    ctx = make_context(
        settings,
        store,
        orchestrator_factory=lambda s, bus, scenario: SlowOrchestrator(bus),  # type: ignore[arg-type,return-value]
    )
    async with api_client(ctx) as client:
        created = (
            await client.post("/api/investigations", json={"question": "x", "scenario": "S1"})
        ).json()
        await asyncio.wait_for(started.wait(), 5)
        running = (await client.get(f"/api/investigations/{created['id']}")).json()
        cancel = await client.post(f"/api/investigations/{created['id']}/cancel")
        frames = parse_sse((await client.get(f"/api/investigations/{created['id']}/events")).text)
        await wait_until_idle(ctx)
        again = await client.post(f"/api/investigations/{created['id']}/cancel")
        final = (await client.get(f"/api/investigations/{created['id']}")).json()
    assert running["status"] == "pending"  # the placeholder until the orchestrator reports
    assert cancel.status_code == 202 and cancel.json()["status"] == "cancelled"
    assert frames[-1]["data"]["data"]["status"] == "cancelled"
    assert final["status"] == "cancelled"
    assert again.status_code == 409 and again.json()["error"]["code"] == "not_running"


async def test_clarification_resumes_on_the_same_stream(
    settings: Settings, store: InvestigationStore
) -> None:
    """Live mode (a key configured, capabilities up) with scripted agents: an ambiguous
    question asks which service; the answer resumes it and the seq numbering continues."""
    from tests.unit.test_orchestrator import orchestrator as scripted_orchestrator

    live_settings = with_llm_key(settings)

    def factory(s: Settings, bus: EventBus, scenario: str | None) -> Any:
        assert scenario is None  # live
        orch, _ = scripted_orchestrator(live_settings)
        orch.bus = bus
        return orch

    ctx = make_context(live_settings, store, orchestrator_factory=factory)
    async with api_client(ctx) as client:
        created = (
            await client.post("/api/investigations", json={"question": "Something is broken"})
        ).json()
        assert created["mode"] == "live"
        first = parse_sse((await client.get(f"/api/investigations/{created['id']}/events")).text)
        await wait_until_idle(ctx)
        waiting = (await client.get(f"/api/investigations/{created['id']}")).json()
        wrong = await client.post("/api/investigations/inv-nope/clarify", json={"answer": "x"})
        resumed = await client.post(
            f"/api/investigations/{created['id']}/clarify", json={"answer": "payment-service"}
        )
        rest = parse_sse(
            (
                await client.get(
                    f"/api/investigations/{created['id']}/events",
                    headers={"Last-Event-ID": first[-1]["id"]},
                )
            ).text
        )
        await wait_until_idle(ctx)
        done = (await client.get(f"/api/investigations/{created['id']}")).json()
        twice = await client.post(
            f"/api/investigations/{created['id']}/clarify", json={"answer": "payment-service"}
        )
    assert [f["event"] for f in first][-2:] == ["clarification_needed", "investigation_finished"]
    assert first[-1]["data"]["data"]["status"] == "needs_clarification"
    assert waiting["status"] == "needs_clarification" and waiting["clarification_candidates"]
    assert wrong.status_code == 404
    assert resumed.status_code == 202
    assert rest[0]["data"]["seq"] == len(first) + 1
    assert rest[-1]["event"] == "investigation_finished"
    assert done["status"] == "completed" and done["context"]["service"] == "payment-service"
    assert done["created_at"] == waiting["created_at"]
    assert twice.status_code == 409


# --------------------------------------------------------------------------- SSE internals


async def test_sse_heartbeat_while_idle_then_live_events(store: InvestigationStore) -> None:
    bus = EventBus()
    bus.publish("investigation_started", "inv-hb", question="q")
    frames: list[str] = []

    async def consume() -> None:
        async for chunk in event_stream(
            "inv-hb",
            after=0,
            store=store,
            bus=bus,
            is_live=lambda _: True,
            heartbeat_s=0.05,
        ):
            frames.append(chunk)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.18)
    bus.publish("investigation_finished", "inv-hb", status="completed", duration_ms=1)
    await asyncio.wait_for(task, 2)
    await store.close()
    parsed = parse_sse("".join(frames))
    types = [f["event"] for f in parsed]
    assert types[0] == "investigation_started" and types[-1] == "investigation_finished"
    beats = [f for f in parsed if f["event"] == "heartbeat"]
    assert len(beats) >= 2 and all(b["data"]["seq"] == 1 for b in beats)
    assert parsed[-1]["data"]["seq"] == 2


async def test_sse_follows_the_store_for_another_process(store: InvestigationStore) -> None:
    """Not running here (e.g. another API process): poll the store until it finishes."""
    inv = stored("inv-other", datetime.now(UTC), status=InvestigationStatus.RUNNING)
    await store.save(inv)
    producer = EventBus()
    await store.save_events([producer.publish("investigation_started", inv.id, question="q")])

    async def finish_later() -> None:
        await asyncio.sleep(0.1)
        await store.save_events(
            [producer.publish("investigation_finished", inv.id, status="completed")]
        )
        await store.save(inv.model_copy(update={"status": InvestigationStatus.COMPLETED}))

    async def collect() -> list[str]:
        out = []
        stream: AsyncIterator[str] = event_stream(
            inv.id,
            after=0,
            store=store,
            bus=EventBus(),
            is_live=lambda _: False,
            heartbeat_s=5,
            poll_s=0.02,
        )
        async for chunk in stream:
            out.append(chunk)
        return out

    chunks, _ = await asyncio.gather(collect(), finish_later())
    await store.close()
    assert [f["event"] for f in parse_sse("".join(chunks))] == [
        "investigation_started",
        "investigation_finished",
    ]


# --------------------------------------------------------------------------- approvals


async def test_ticket_draft_approve_and_deny(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        inv_id, _ = await run_replay(client, question="x", scenario="S1")
        await wait_until_idle(ctx)
        drafted = await client.post(f"/api/investigations/{inv_id}/tickets/draft")
        assert drafted.status_code == 201, drafted.text
        assert "pool" in drafted.json()["summary"].lower()
        assert "computed for you" not in drafted.json()["summary"]
        second = await client.post(
            f"/api/investigations/{inv_id}/tickets/draft", json={"requested_by": "bob"}
        )
        pending = (await client.get("/api/approvals?status=pending")).json()
        approved = await client.post(
            f"/api/approvals/{drafted.json()['approval_id']}/approve",
            json={"by": "alice", "comment": "ship it"},
        )
        again = await client.post(
            f"/api/approvals/{drafted.json()['approval_id']}/approve", json={"by": "alice"}
        )
        denied = await client.post(
            f"/api/approvals/{second.json()['approval_id']}/deny", json={"by": "carol"}
        )
        missing = await client.post("/api/approvals/act-nope/deny", json={"by": "carol"})
        no_by = await client.post(
            f"/api/approvals/{second.json()['approval_id']}/deny", json={"by": ""}
        )
        everything = (await client.get("/api/approvals")).json()
        bad_status = await client.get("/api/approvals?status=bogus")
    assert drafted.status_code == 201 and drafted.json()["status"] == "pending"
    assert drafted.json()["summary"]
    assert [p["id"] for p in pending] == [
        second.json()["approval_id"],
        drafted.json()["approval_id"],
    ]
    first = next(p for p in pending if p["id"] == drafted.json()["approval_id"])
    assert first["tool"] == "jira_create_issue" and first["investigation_id"] == inv_id
    assert first["arguments"]["project_key"] == "OPS"
    body = approved.json()
    assert approved.status_code == 200 and body["status"] == "executed"
    assert body["decided_by"] == "alice" and body["comment"] == "ship it"
    assert body["result"]["issue"]["key"] == "OPS-999"
    assert again.status_code == 409 and again.json()["error"]["code"] == "invalid_state"
    assert denied.json()["status"] == "denied" and denied.json()["decided_by"] == "carol"
    assert missing.status_code == 404
    assert no_by.status_code == 422
    assert {p["lifecycle_status"] for p in everything} == {"executed", "denied"}
    assert bad_status.status_code == 422


async def test_policy_rejection_and_nothing_to_draft(settings: Settings, ctx: ApiContext) -> None:
    await ctx.store.save(stored("inv-empty", datetime.now(UTC)))
    async with api_client(ctx) as client:
        empty = await client.post("/api/investigations/inv-empty/tickets/draft")
    assert empty.status_code == 409 and empty.json()["error"]["code"] == "nothing_to_draft"
    rejected = ctx.approvals.propose(
        action="create_ticket",
        capability="tickets",
        tool="jira_delete_everything",
        arguments={},
        reason="x",
        requested_by="t",
    )
    async with api_client(ctx) as client:
        listed = (await client.get("/api/approvals")).json()
    row = next(p for p in listed if p["id"] == rejected.id)
    assert row["status"] == "denied" and row["lifecycle_status"] == "rejected"
    assert row["policy_reason"]


# --------------------------------------------------------------------------- scenarios


async def test_scenarios_are_listed_and_faults_forbidden_by_default(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        listed = (await client.get("/api/scenarios")).json()
        inject = await client.post("/api/scenarios/S1/inject")
        revert = await client.post("/api/scenarios/revert")
    assert [s["id"] for s in listed] == ["S0", "S1", "S2", "S3", "S4", "S5"]
    assert listed[0]["injectable"] is False and all(not s["active"] for s in listed)
    assert inject.status_code == 403 and inject.json()["error"]["code"] == "faults_disabled"
    assert revert.status_code == 403


async def test_fault_endpoints_when_enabled(
    settings: Settings, store: InvestigationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOPS_ENABLE_FAULTS", "1")
    faults = FakeFaults()
    ctx = make_context(settings, store, faults=faults)
    async with api_client(ctx) as client:
        injected = await client.post("/api/scenarios/s1/inject")
        active = (await client.get("/api/scenarios")).json()
        busy = await client.post("/api/scenarios/S2/inject")
        healthy = await client.post("/api/scenarios/S0/inject")
        unknown = await client.post("/api/scenarios/S9/inject")
        reverted = await client.post("/api/scenarios/revert")
    assert injected.status_code == 200 and injected.json()["scenario"] == "S1"
    assert [s["id"] for s in active if s["active"]] == ["S1"]
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "fault_error"
    assert healthy.status_code == 422 and unknown.status_code == 404
    assert reverted.status_code == 202 and reverted.json()["status"] == "reverting"
    assert faults.calls == ["inject S1", "revert"]


def test_scenario_matching(settings: Settings) -> None:
    catalog = ServiceCatalog.from_settings(settings)
    scenarios = ScenarioCatalog.load(CONFIG)
    match = scenarios.match

    def found(q: str, svc: str | None = None) -> str | None:
        s = match(q, svc, catalog)
        return s.id if s else None

    assert found("Payment API is returning HTTP 500 in production") == "S1"
    assert found("payments api timeouts, db pool?") == "S1"
    assert found("Redis cache down, payment latency up") == "S5"
    assert found("why is it slow", "user-service") == "S4"
    assert found("hello world") is None
    assert found("anything", "unknown-service") is None


# --------------------------------------------------------------------------- cross-cutting


async def test_api_key_guard(settings: Settings, store: InvestigationStore) -> None:
    tuned = settings.model_copy(update={"api": ApiConfig(api_key=SecretStr(FAKE_API_KEY))})
    ctx = make_context(tuned, store)
    async with api_client(ctx) as client:
        health = await client.get("/api/health")
        docs = await client.get("/api/openapi.json")
        denied = await client.get("/api/services")
        wrong = await client.get("/api/services", headers={"X-API-Key": "nope"})
        ok = await client.get("/api/services", headers={"X-API-Key": FAKE_API_KEY})
        sse_query = await client.get(f"/api/investigations/inv-x/events?api_key={FAKE_API_KEY}")
        query_elsewhere = await client.get(f"/api/services?api_key={FAKE_API_KEY}")
    assert health.status_code == 200 and docs.status_code == 200
    assert denied.status_code == 401 and denied.json()["error"]["code"] == "unauthorized"
    assert wrong.status_code == 401 and ok.status_code == 200
    assert sse_query.status_code == 404  # authorized, then: unknown investigation
    assert query_elsewhere.status_code == 401


async def test_request_id_cors_and_openapi(ctx: ApiContext) -> None:
    async with api_client(ctx) as client:
        echoed = await client.get("/api/health", headers={"X-Request-ID": "req-123"})
        generated = await client.get("/api/health")
        preflight = await client.options(
            "/api/investigations",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        foreign = await client.get("/api/health", headers={"Origin": "http://evil.example"})
        spec = (await client.get("/api/openapi.json")).json()
        docs = await client.get("/api/docs")
    assert echoed.headers["x-request-id"] == "req-123"
    assert len(generated.headers["x-request-id"]) == 32
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:3000"
    assert "access-control-allow-origin" not in foreign.headers
    paths = set(spec["paths"])
    assert {
        "/api/health",
        "/api/services",
        "/api/agents",
        "/api/dashboard/summary",
        "/api/investigations",
        "/api/investigations/{investigation_id}",
        "/api/investigations/{investigation_id}/events",
        "/api/investigations/{investigation_id}/report.md",
        "/api/investigations/{investigation_id}/clarify",
        "/api/investigations/{investigation_id}/cancel",
        "/api/investigations/{investigation_id}/tickets/draft",
        "/api/approvals",
        "/api/approvals/{approval_id}/approve",
        "/api/approvals/{approval_id}/deny",
        "/api/scenarios",
        "/api/scenarios/{scenario_id}/inject",
        "/api/scenarios/revert",
    } <= paths
    assert docs.status_code == 200


async def test_store_down_answers_503(settings: Settings, tmp_path: Path) -> None:
    store = InvestigationStore("postgresql+psycopg://x:y@127.0.0.1:1/none")
    ctx = make_context(settings, store)
    async with api_client(ctx) as client:
        health = (await client.get("/api/health")).json()
        listed = await client.get("/api/investigations")
    assert health["status"] == "degraded" and health["store"] == "down"
    assert listed.status_code == 503
    assert listed.json()["error"]["code"] == "store_unavailable"


def test_runner_default_limit_comes_from_the_profile(settings: Settings, tmp_path: Path) -> None:
    runner = InvestigationRunner(settings, InvestigationStore("sqlite://"), EventBus())
    assert runner.max_running == settings.api.max_running_investigations == 2
