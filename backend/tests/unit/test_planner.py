"""Planner (PR-030): deterministic parsing of 20+ phrasings, clarification, plan DAG."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import aiops.agents  # noqa: F401  (registers built-in agents)
from aiops.agents.registry import AGENTS
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import AgentConfig, OrchestratorConfig, Settings, load_settings
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.orchestrator.planner import (
    Plan,
    Planner,
    PlanRequest,
    find_services,
    find_symptoms,
    parse_time_range,
)

CONFIG = Path(__file__).resolve().parents[3] / "config"
NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


@pytest.fixture(scope="module")
def catalog(settings: Settings) -> ServiceCatalog:
    return ServiceCatalog.from_settings(settings)


def plan_for(
    settings: Settings, question: str, llm: FakeLLMProvider | None = None, **kwargs: object
) -> Plan:
    planner = Planner(settings, ServiceCatalog.from_settings(settings), AGENTS, llm=llm)
    return asyncio.run(planner.plan(PlanRequest(question=question, now=NOW, **kwargs)))  # type: ignore[arg-type]


# 20 phrasings: (question, service, environment, window start or None for default 30m)
PHRASINGS: list[tuple[str, str, str, datetime | None]] = [
    ("Payment API is returning HTTP 500 in production", "payment-service", "production", None),
    ("payments are slow in prod", "payment-service", "production", None),
    ("Why are orders timing out in production?", "order-service", "production", None),
    ("Orders are failing intermittently in production", "order-service", "production", None),
    (
        "inventory-service latency since 10:15",
        "inventory-service",
        "production",
        NOW.replace(minute=15),
    ),
    ("users can't login on staging", "user-service", "staging", None),
    (
        "Is anything wrong with payment-service in production?",
        "payment-service",
        "production",
        None,
    ),
    (
        "payment-service errors in the last 2 hours",
        "payment-service",
        "production",
        NOW - timedelta(hours=2),
    ),
    ("checkout 503s for the last hour", "order-service", "production", NOW - timedelta(hours=1)),
    (
        "stock service down past 45 min",
        "inventory-service",
        "production",
        NOW - timedelta(minutes=45),
    ),
    (
        "auth failing since 09:50Z in prd",
        "user-service",
        "production",
        NOW.replace(hour=9, minute=50),
    ),
    (
        "pay api 500 since 2026-09-25T08:00Z",
        "payment-service",
        "production",
        NOW.replace(hour=8, minute=0),
    ),
    (
        "Orders API high latency last 15m",
        "order-service",
        "production",
        NOW - timedelta(minutes=15),
    ),
    ("order-service calls to inventory-service time out", "order-service", "production", None),
    (
        "The payments-api started failing 20 minutes ago",
        "payment-service",
        "production",
        NOW - timedelta(minutes=25),
    ),
    ("user service OOM restarts in stage", "user-service", "staging", None),
    (
        "inventory errors between 10:00 and 10:20",
        "inventory-service",
        "production",
        NOW.replace(minute=0),
    ),
    ("PAYMENT-SERVICE HTTP 5xx on live", "payment-service", "production", None),
    ("accounts slow over the last 3h", "user-service", "production", NOW - timedelta(hours=3)),
    (
        "payment service crashloop after the deployment last 1d",
        "payment-service",
        "production",
        NOW - timedelta(days=1),
    ),
]


@pytest.mark.parametrize(("question", "service", "environment", "start"), PHRASINGS)
def test_twenty_phrasings(
    settings: Settings, question: str, service: str, environment: str, start: datetime | None
) -> None:
    plan = plan_for(settings, question)
    assert not plan.needs_clarification, plan.clarification_question
    assert plan.context.service == service
    assert plan.context.environment == environment
    expected = start or NOW - timedelta(minutes=30)
    assert plan.context.time_range.start == expected
    assert plan.context.time_range.end == (NOW.replace(minute=20) if "between" in question else NOW)


@pytest.mark.parametrize(
    "question",
    ["Something is broken", "The API is down", "everything is slow since 10:00", "help!"],
)
def test_unknown_service_needs_clarification_never_invents(
    settings: Settings, question: str
) -> None:
    plan = plan_for(settings, question)
    assert plan.needs_clarification
    assert plan.context.service is None
    assert plan.steps == []
    assert set(plan.candidates) == {
        "payment-service",
        "order-service",
        "user-service",
        "inventory-service",
    }
    assert plan.clarification_question and "since when" in plan.clarification_question


def test_ambiguous_services_ask_with_candidates(settings: Settings) -> None:
    plan = plan_for(settings, "Login and checkout requests are failing in production")
    assert plan.needs_clarification
    assert plan.candidates == ["user-service", "order-service"]
    assert "user-service, order-service" in (plan.clarification_question or "")


def test_explicit_service_wins_and_unknown_one_is_not_invented(settings: Settings) -> None:
    assert plan_for(settings, "It is broken", service="payments").context.service == (
        "payment-service"
    )
    plan = plan_for(settings, "It is broken", service="billing-service")
    assert plan.needs_clarification
    assert "billing-service" not in plan.candidates


def test_llm_fallback_only_accepts_catalog_services(settings: Settings) -> None:
    # The fast model names an alias -> resolved through the catalog.
    llm = FakeLLMProvider([tool_call("submit", {"service": "stock", "since": "2h"})])
    plan = plan_for(settings, "the thing that tracks what we can sell is slow", llm=llm)
    assert plan.context.service == "inventory-service"
    assert plan.parsed.service_source == "llm"
    assert plan.context.time_range.start == NOW - timedelta(hours=2)
    assert llm.requests[0]["role"] == "fast"
    # ... an invented service is never accepted.
    llm = FakeLLMProvider([tool_call("submit", {"service": "billing-service"})])
    plan = plan_for(settings, "the thing that bills people is slow", llm=llm)
    assert plan.needs_clarification and plan.context.service is None


def test_llm_errors_fall_back_to_clarification(settings: Settings) -> None:
    plan = plan_for(settings, "Something is broken", llm=FakeLLMProvider())  # empty script
    assert plan.needs_clarification
    assert any("unavailable" in note for note in plan.notes)


def test_rules_first_no_llm_call_when_service_found(settings: Settings) -> None:
    llm = FakeLLMProvider()
    plan_for(settings, "Payment API is returning HTTP 500", llm=llm)
    assert llm.requests == []


def test_plan_is_a_dag_from_registry_and_profile(settings: Settings) -> None:
    plan = plan_for(settings, "Payment API is returning HTTP 500 in production")
    round1 = {s.agent for s in plan.round(1)}
    round2 = {s.agent for s in plan.round(2)}
    assert round1 == {"logs", "metrics", "alerts", "k8s", "code"}  # code + k8s always round 1
    assert round2 == {"knowledge", "tickets"}
    ids = {s.id for s in plan.round(1)}
    assert all(set(s.depends_on) == ids for s in plan.round(2))
    assert all(s.depends_on == [] for s in plan.round(1))
    assert "rca" not in round1 | round2


def test_plan_only_uses_enabled_capabilities_and_agents(settings: Settings) -> None:
    caps = dict(settings.capabilities)
    caps["k8s"] = caps["k8s"].model_copy(update={"enabled": False})
    agents = {**settings.agents, "tickets": AgentConfig(enabled=False)}
    trimmed = settings.model_copy(update={"capabilities": caps, "agents": agents})
    plan = plan_for(trimmed, "Payment API is returning HTTP 500")
    names = {s.agent for s in plan.steps}
    assert "k8s" not in names and "tickets" not in names
    single_round = trimmed.model_copy(update={"orchestrator": OrchestratorConfig(max_rounds=1)})
    assert {s.round for s in plan_for(single_round, "payments 500").steps} == {1}


def test_symptoms_and_helpers(catalog: ServiceCatalog) -> None:
    assert find_symptoms("Payment API is returning HTTP 500") == ["http_5xx"]
    assert find_symptoms("orders timing out and slow") == ["latency", "timeouts"]
    assert find_services("order-service calls inventory-service", catalog) == [
        "order-service",
        "inventory-service",
    ]
    window, source = parse_time_range("since 11:00", NOW, "30m")  # future -> yesterday
    assert source == "since" and window.start == NOW.replace(hour=11, minute=0) - timedelta(days=1)
    assert parse_time_range("no time here", NOW, "45m")[0].start == NOW - timedelta(minutes=45)
