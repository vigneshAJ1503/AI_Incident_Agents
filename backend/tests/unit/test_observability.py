"""AI observability + cost controls (PR-041): pricing, the instrumented LLM, spans (in-memory
exporter), Prometheus metrics, budgets, the investigation tool cache and /metrics.
Zero tokens: every LLM is the scripted FakeLLMProvider."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY

from aiops.agents.base import AgentDeps, AgentSpec, BaseAgent
from aiops.agents.registry import AgentRegistry
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import (
    CostConfig,
    EvalsConfig,
    MetricsConfig,
    ModelPrice,
    ObservabilityConfig,
    OrchestratorConfig,
    Settings,
    TracingConfig,
    load_settings,
)
from aiops.core.events import EventBus, EventSink
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    EvidenceKind,
    Investigation,
    InvestigationReport,
    InvestigationStatus,
    TokenUsage,
)
from aiops.llm.base import LLMRateLimitError, LLMResponse
from aiops.llm.fake import FakeLLMProvider, text
from aiops.mcp.registry import MCPRegistry
from aiops.observability import metrics, tracing
from aiops.observability.budget import InvestigationBudget, current_scope, llm_scope
from aiops.observability.cache import ToolCallCache, cache_key
from aiops.observability.llm import InstrumentedLLM, instrument
from aiops.observability.pricing import cost_usd, price_for, priced
from aiops.orchestrator.dashboard import agent_stats, cost_summary
from aiops.orchestrator.engine import InvestigationRequest, InvestigationRun, Orchestrator
from aiops.store.repository import InvestigationStore
from tests.api_support import api_client, make_context
from tests.unit.agent_helpers import (
    EchoAgent,
    make_deps,
    make_prompts,
    make_settings,
    make_task,
    search_then_submit,
)

CONFIG = Path(__file__).resolve().parents[3] / "config"
NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
#: USD per 1M tokens for the scripted model: 10 in / 5 out per call -> 20 µ$ per call.
FAKE_PRICE = ModelPrice(input=1.0, output=2.0)


def sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def with_prices(settings: Settings, **prices: ModelPrice) -> Settings:
    return settings.model_copy(update={"cost": CostConfig(pricing=prices)})


@pytest.fixture
def spans() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.use_provider(provider)
    try:
        yield exporter
    finally:
        tracing.use_provider(None)
        provider.shutdown()


def by_name(finished: tuple[ReadableSpan, ...], name: str) -> ReadableSpan:
    return next(s for s in finished if s.name == name)


# --------------------------------------------------------------------------- pricing


def test_price_lookup_order_and_cost() -> None:
    settings = load_settings("local", CONFIG)
    assert price_for(settings, "some-model") == ModelPrice()  # free tier default: $0
    table = {
        "*": ModelPrice(input=0.1, output=0.1),
        "api.groq.com": ModelPrice(input=0.5, output=0.5),
        "paid-model": ModelPrice(input=3.0, output=15.0),
    }
    priced_settings = with_prices(settings, **table)
    assert price_for(priced_settings, "paid-model").output == 15.0
    assert price_for(priced_settings, "other").input == 0.5  # provider host (groq)
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=200_000, calls=3)
    assert cost_usd(ModelPrice(input=3.0, output=15.0), usage) == pytest.approx(6.0)
    assert priced(priced_settings, "paid-model", usage).cost_usd == pytest.approx(6.0)


def test_cost_pricing_wins_over_the_deprecated_evals_pricing() -> None:
    settings = load_settings("local", CONFIG).model_copy(
        update={
            "evals": EvalsConfig(pricing={"m": ModelPrice(input=9), "old": ModelPrice(input=1)}),
            "cost": CostConfig(pricing={"m": ModelPrice(input=2)}),
        }
    )
    assert price_for(settings, "m").input == 2
    assert price_for(settings, "old").input == 1  # still read


def test_token_usage_sums_cost() -> None:
    total = TokenUsage(input_tokens=1, cost_usd=0.25) + TokenUsage(output_tokens=2, cost_usd=0.5)
    assert total.cost_usd == pytest.approx(0.75) and total.total_tokens == 3


# --------------------------------------------------------------------------- instrumented LLM


async def test_llm_calls_are_priced_budgeted_measured_and_traced(
    spans: InMemorySpanExporter,
) -> None:
    settings = with_prices(load_settings("local", CONFIG), fake=FAKE_PRICE)
    llm = instrument(FakeLLMProvider([text("hi")]), settings)
    assert instrument(llm, settings) is llm  # wrapping twice is a no-op
    budget = InvestigationBudget(max_tokens=1000)
    labels = {"agent": "logs", "role": "fast", "model": "fake"}
    before = sample("aiops_llm_tokens_total", direction="input", **labels)
    before_cost = sample("aiops_llm_cost_usd_total", **labels)
    with llm_scope("logs", budget):
        response = await llm.generate([], role="fast")
    assert current_scope().agent == "orchestrator"  # the scope is restored
    assert response.usage.cost_usd == pytest.approx(20e-6)
    assert budget.spent.total_tokens == 15 and budget.by_agent["logs"].calls == 1
    assert sample("aiops_llm_tokens_total", direction="input", **labels) - before == 10
    assert sample("aiops_llm_cost_usd_total", **labels) - before_cost == pytest.approx(20e-6)
    assert sample("aiops_llm_requests_total", status="ok", **labels) >= 1
    span = by_name(spans.get_finished_spans(), "chat fake")
    attrs = dict(span.attributes or {})
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.usage.input_tokens"] == 10 and attrs["gen_ai.usage.output_tokens"] == 5
    assert attrs["aiops.llm.role"] == "fast" and attrs["aiops.agent"] == "logs"
    assert attrs["aiops.cost_usd"] == pytest.approx(20e-6)
    assert not any("messages" in key or "prompt" in key for key in attrs)  # no content


async def test_llm_errors_are_counted_by_kind() -> None:
    class RateLimited(FakeLLMProvider):
        async def generate(self, *args: Any, **kwargs: Any) -> LLMResponse:
            raise LLMRateLimitError("429")

    llm = InstrumentedLLM(RateLimited(), load_settings("local", CONFIG))
    labels = {"agent": "rca", "role": "rca", "model": "fake", "status": "rate_limited"}
    before = sample("aiops_llm_requests_total", **labels)
    with llm_scope("rca"), pytest.raises(LLMRateLimitError):
        await llm.generate([], role="rca")
    assert sample("aiops_llm_requests_total", **labels) - before == 1


def test_configured_model_is_the_routed_role() -> None:
    settings = load_settings("local", CONFIG)
    llm = settings.llm.model_copy(update={"models": {"fast": "small", "agent": "big"}})
    routed = InstrumentedLLM(text_provider(), settings.model_copy(update={"llm": llm}))
    assert routed.configured_model("fast") == "small"
    assert routed.configured_model("rca") == "big"  # falls back to the agent model


def text_provider() -> Any:
    class Named(FakeLLMProvider):
        @property
        def name(self) -> str:
            return "openai_compat"

    return Named()


# --------------------------------------------------------------------------- budgets


def test_investigation_budget_tokens_and_cost() -> None:
    budget = InvestigationBudget(max_tokens=100, max_cost_usd=0.01)
    budget.record("logs", TokenUsage(input_tokens=50, cost_usd=0.005))
    assert not budget.exhausted
    budget.record("metrics", TokenUsage(input_tokens=10, cost_usd=0.006))
    assert budget.exhausted and budget.describe() == "cost budget ($0.01)"
    tokens = InvestigationBudget(max_tokens=10)
    tokens.record("logs", TokenUsage(output_tokens=10, cost_usd=5.0))  # no cost cap
    assert tokens.exhausted and tokens.describe() == "token budget (10)"
    run = InvestigationRun(Investigation.model_validate(_investigation()), budget=tokens)
    assert run.budget_exhausted(max_tokens=1_000)


async def test_spent_investigation_budget_finishes_the_agent_deterministically(
    tmp_path: Path,
) -> None:
    llm = FakeLLMProvider(responder=search_then_submit)
    deps, _, _ = make_deps(tmp_path, llm)
    deps.budget = InvestigationBudget(max_tokens=100, max_cost_usd=0.5)
    deps.budget.record("metrics", TokenUsage(input_tokens=1, cost_usd=0.5))
    before = sample("aiops_agent_fallbacks_total", agent="echo", reason="investigation_budget")
    result = await EchoAgent(deps).run(make_task())
    assert llm.requests == []  # the real LLM is never called
    assert result.status is AgentStatus.NO_SIGNAL
    assert any("cost budget ($0.5) is spent" in f for f in result.suggested_followups)
    assert sample("aiops_agent_fallbacks_total", agent="echo", reason="investigation_budget") == (
        before + 1
    )


# --------------------------------------------------------------------------- tool cache


def counting_server(calls: list[str]) -> MCPServer:
    server = MCPServer("counting-logs")

    @server.tool()
    def search_logs(service: str) -> dict[str, Any]:
        """Search logs."""
        calls.append(service)
        if service == "broken":
            raise ValueError("backend error")
        return {"service": service, "errors": len(calls)}

    return server


def test_cache_ttl_and_lru() -> None:
    now = [0.0]
    cache = ToolCallCache(ttl_s=10, max_entries=2, clock=lambda: now[0])
    ok = _outcome(ok=True)
    first, second, third = (cache_key("inv", "logs", "t", {"n": i}) for i in range(3))
    cache.put(first, ok)
    assert cache.get(first) is ok
    now[0] = 11
    assert cache.get(first) is None and len(cache) == 0  # expired
    cache.put(first, ok)
    cache.put(second, ok)
    cache.put(third, ok)
    assert cache.get(first) is None and cache.get(third) is ok  # LRU evicted the oldest
    cache.put(first, _outcome(ok=False))
    assert cache.get(first) is None  # failures are never cached
    assert cache_key("a", "logs", "t", {"x": 1, "y": 2}) == cache_key(
        "a", "logs", "t", {"y": 2, "x": 1}
    )
    off = ToolCallCache(ttl_s=0)
    off.put(first, ok)
    assert not off.enabled and off.get(first) is None


async def test_identical_read_only_calls_hit_the_cache_once(spans: InMemorySpanExporter) -> None:
    settings = make_settings()
    calls: list[str] = []
    audit = MemoryAuditSink()
    registry = MCPRegistry(settings, audit=audit, overrides={"logs": counting_server(calls)})
    registry.tool_cache = ToolCallCache(ttl_s=60)
    labels = {"capability": "logs", "tool": "search_logs", "status": "ok"}
    before_hits = sample("aiops_tool_calls_total", cached="true", **labels)
    async with registry.toolset("logs", agent="logs", investigation_id="inv-a") as toolset:
        first = await toolset.call("search_logs", {"service": "payment-service"})
        again = await toolset.call("search_logs", {"service": "payment-service"})
        other = await toolset.call("search_logs", {"service": "order-service"})
        broken = await toolset.call("search_logs", {"service": "broken"})
        broken_again = await toolset.call("search_logs", {"service": "broken"})
        unlisted = await toolset.call("drop_everything", {})
    async with registry.toolset("logs", agent="metrics", investigation_id="inv-b") as other_inv:
        fresh = await other_inv.call("search_logs", {"service": "payment-service"})
    assert calls == ["payment-service", "order-service", "broken", "broken", "payment-service"]
    assert not first.tool_call.cached and again.tool_call.cached and not fresh.tool_call.cached
    assert again.data == first.data and again.data is not first.data
    assert again.tool_call.id != first.tool_call.id  # a new, audited call
    assert not other.tool_call.cached and not broken_again.tool_call.cached
    assert broken.tool_call.status == "error" and unlisted.tool_call.status == "blocked"
    assert len(audit.records) == 7 and audit.records[1].tool_call.cached
    assert sample("aiops_tool_calls_total", cached="true", **labels) - before_hits == 1
    assert (
        sample(
            "aiops_tool_calls_total",
            capability="logs",
            tool=metrics.UNLISTED_TOOL,  # bounded label: never the model's invented name
            status="blocked",
            cached="false",
        )
        >= 1
    )
    names = [s.name for s in spans.get_finished_spans()]
    assert "execute_tool _unlisted" in names and "execute_tool drop_everything" not in names


async def test_no_cache_without_an_investigation_or_for_writes() -> None:
    settings = make_settings()
    calls: list[str] = []
    registry = MCPRegistry(
        settings, audit=MemoryAuditSink(), overrides={"logs": counting_server(calls)}
    )
    registry.tool_cache = ToolCallCache(ttl_s=60)
    async with registry.toolset("logs", agent="cli") as toolset:  # no investigation id
        await toolset.call("search_logs", {"service": "a"})
        repeated = await toolset.call("search_logs", {"service": "a"})
    assert calls == ["a", "a"] and not repeated.tool_call.cached


# --------------------------------------------------------------------------- end to end


class PricedLogsAgent(BaseAgent):
    """A real BaseAgent (LLM tool loop) named ``logs`` for an orchestrated run."""

    spec = AgentSpec(
        name="logs",
        version="1",
        description="Test logs agent",
        capabilities=["logs"],
        evidence_kind=EvidenceKind.LOG,
        prompt="echo",
    )


async def test_orchestrated_investigation_cost_spans_and_metrics(
    tmp_path: Path, spans: InMemorySpanExporter
) -> None:
    settings = with_prices(make_settings(), fake=FAKE_PRICE)
    llm = FakeLLMProvider(responder=search_then_submit)
    prompts = make_prompts(tmp_path / "prompts")
    calls: list[str] = []
    registry = AgentRegistry()
    registry.register(PricedLogsAgent)

    def deps(agent: str, service: str | None, events: EventSink) -> AgentDeps:
        return AgentDeps(
            settings=settings,
            llm=llm,
            mcp=MCPRegistry(
                settings, audit=MemoryAuditSink(), overrides={"logs": counting_server(calls)}
            ),
            prompts=prompts,
            catalog=ServiceCatalog.from_settings(settings),
            events=events,
        )

    async def no_rca(run: Any) -> None:
        return None

    bus = EventBus()
    orch = Orchestrator(
        settings, llm=llm, bus=bus, registry=registry, deps_factory=deps, concluder=no_rca
    )
    runs_before = sample("aiops_agent_runs_total", agent="logs", status="success")
    inv = await orch.investigate(
        InvestigationRequest(question="Payment API is returning HTTP 500", end=NOW)
    )
    assert inv.status is InvestigationStatus.COMPLETED, inv
    result = inv.results[0]
    # 2 LLM calls x (10 in + 5 out) at $1/$2 per 1M tokens.
    assert result.usage.cost_usd == pytest.approx(40e-6)
    assert inv.usage.cost_usd == pytest.approx(40e-6)
    finished = [e for e in bus.history(inv.id) if e.type == "agent_finished"]
    assert finished[0].data["cost_usd"] == pytest.approx(40e-6)
    assert sample("aiops_agent_runs_total", agent="logs", status="success") == runs_before + 1

    done = spans.get_finished_spans()
    root = by_name(done, "investigation")
    agent = by_name(done, "invoke_agent logs")
    chat = by_name(done, "chat fake")
    tool = by_name(done, "execute_tool search_logs")
    assert root.parent is None
    assert by_name(done, "plan").parent.span_id == root.context.span_id  # type: ignore[union-attr]
    assert by_name(done, "rca").parent.span_id == root.context.span_id  # type: ignore[union-attr]
    assert agent.parent.span_id == root.context.span_id  # type: ignore[union-attr]
    assert chat.parent.span_id == agent.context.span_id  # type: ignore[union-attr]
    assert tool.parent.span_id == agent.context.span_id  # type: ignore[union-attr]
    assert {s.context.trace_id for s in done} == {root.context.trace_id}
    root_attrs = dict(root.attributes or {})
    assert root_attrs["aiops.investigation.id"] == inv.id
    assert root_attrs["aiops.status"] == "completed"
    assert root_attrs["aiops.cost_usd"] == pytest.approx(40e-6)
    agent_attrs = dict(agent.attributes or {})
    assert agent_attrs["gen_ai.agent.name"] == "logs" and agent_attrs["aiops.llm.role"] == "agent"
    assert agent_attrs["gen_ai.usage.input_tokens"] == 20


async def test_replay_investigation_stays_at_zero_tokens_and_cost() -> None:
    from aiops.orchestrator.replay import load_replay

    settings = with_prices(load_settings("local", CONFIG), **{"*": ModelPrice(input=5, output=5)})
    orch = Orchestrator(settings, replay=load_replay(settings, "S1"), bus=EventBus())
    inv = await orch.investigate(InvestigationRequest(question="", mode="replay"))
    assert inv.usage.total_tokens == 0 and inv.usage.cost_usd == 0.0
    cached = [c for r in inv.results for c in r.tool_calls if c.cached]
    assert all(c.status == "ok" for c in cached)


# --------------------------------------------------------------------------- tracing config


def test_tracing_is_off_without_an_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    assert tracing.endpoint(TracingConfig()) is None
    assert tracing.setup_tracing(TracingConfig()) is False
    assert tracing.endpoint(TracingConfig(otlp_endpoint="http://jaeger:4318/")) == (
        "http://jaeger:4318/v1/traces"
    )
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/v1/traces")
    assert tracing.endpoint(TracingConfig()) == "http://collector:4318/v1/traces"
    with pytest.raises(ValueError, match="http"):
        TracingConfig(otlp_endpoint="jaeger:4318")


# --------------------------------------------------------------------------- dashboard + API


def _investigation(cost: float = 0.0, **update: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "incident": {"title": "x"},
        "status": "completed",
        "usage": {"input_tokens": 100, "output_tokens": 50, "calls": 2, "cost_usd": cost},
    }
    base.update(update)
    return base


def test_dashboard_cost_per_agent_and_investigation() -> None:
    result = AgentResult(
        agent="logs",
        task_id="t",
        status=AgentStatus.SUCCESS,
        summary="s",
        usage=TokenUsage(input_tokens=10, output_tokens=5, calls=1, cost_usd=0.003),
    )
    invs = [
        Investigation.model_validate(_investigation(0.004)).model_copy(
            update={"results": [result]}
        ),
        Investigation.model_validate(_investigation(0.002)).model_copy(
            update={"results": [result]}
        ),
    ]
    stats = {a["name"]: a for a in agent_stats(invs)}
    assert stats["logs"]["cost_usd"] == pytest.approx(0.006)
    assert stats["logs"]["avg_cost_usd"] == pytest.approx(0.003)
    summary = cost_summary(invs)
    assert summary["total_usd"] == pytest.approx(0.006)
    assert summary["avg_usd_per_investigation"] == pytest.approx(0.003)
    assert summary["avg_tokens_per_investigation"] == 150
    assert cost_summary([])["avg_usd_per_investigation"] == 0.0


def test_investigation_metrics() -> None:
    inv = Investigation.model_validate(
        _investigation(0.01, mode="replay", duration_ms=1500.0)
    ).model_copy(
        update={
            "report": InvestigationReport(
                summary="s", root_cause_hypothesis_id="hy-1", confidence=0.9
            )
        }
    )
    before = sample("aiops_investigations_total", mode="replay", status="completed")
    count = sample("aiops_investigation_confidence_count", mode="replay")
    metrics.record_investigation(inv)
    assert sample("aiops_investigations_total", mode="replay", status="completed") == before + 1
    assert sample("aiops_investigation_confidence_count", mode="replay") == count + 1


async def test_metrics_endpoint_and_override_rate(tmp_path: Path) -> None:
    settings = load_settings("local", CONFIG)
    store = InvestigationStore(f"sqlite:///{tmp_path / 'api.db'}")
    store.migrate()
    ctx = make_context(settings, store)
    proposal = ctx.approvals.propose(
        action="create_ticket",
        capability="tickets",
        tool="jira_create_issue",
        arguments={"project_key": "OPS", "summary": "x"},
        reason="test",
        requested_by="bob",
        risk="medium",
    )
    denied_before = sample(
        "aiops_approval_decisions_total", action="create_ticket", decision="denied"
    )
    async with api_client(ctx) as client:
        denied = await client.post(f"/api/approvals/{proposal.id}/deny", json={"by": "carol"})
        scraped = await client.get("/metrics")
        api_health = await client.get("/api/health")
    assert denied.status_code == 200
    assert scraped.status_code == 200
    assert scraped.headers["content-type"].startswith("text/plain")
    body = scraped.text
    for name in (
        "aiops_llm_requests_total",
        "aiops_tool_calls_total",
        "aiops_agent_duration_seconds",
        "aiops_approval_decisions_total",
        "aiops_investigation_cost_usd",
    ):
        assert name in body
    assert "inv-" not in body  # no investigation ids as labels
    assert api_health.status_code == 200
    assert sample("aiops_approval_decisions_total", action="create_ticket", decision="denied") == (
        denied_before + 1
    )

    off = settings.model_copy(
        update={"observability": ObservabilityConfig(metrics=MetricsConfig(enabled=False))}
    )
    async with api_client(make_context(off, store)) as client:
        assert (await client.get("/metrics")).status_code == 404


def test_orchestrator_config_validates_the_new_controls() -> None:
    config = OrchestratorConfig(max_cost_usd=0.5, tool_cache_ttl_s=0)
    assert config.max_cost_usd == 0.5 and config.tool_cache_ttl_s == 0
    with pytest.raises(ValueError):
        OrchestratorConfig(max_cost_usd=-1)


def _outcome(*, ok: bool) -> Any:
    from aiops.core.models import ToolCall
    from aiops.mcp.toolset import ToolOutcome

    call = ToolCall(agent="a", capability="logs", tool="t", status="ok" if ok else "error")
    return ToolOutcome(tool_call=call, ok=ok, content="c", data={"k": 1})
