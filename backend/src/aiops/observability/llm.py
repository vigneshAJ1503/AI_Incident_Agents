"""The LLM wrapper that makes model routing and spend observable (PR-041).

Every call through it:

* is priced: ``response.usage.cost_usd`` from ``cost.pricing`` at the answering model's
  price, so the cost flows through ``TokenUsage`` everywhere tokens already flow (agent
  results, the planner, the RCA, ``Investigation.usage``);
* is recorded in the caller's investigation budget (``observability.budget``);
* gets a ``chat {model}`` span (GenAI conventions: model, role, tokens, cost, status);
* updates the Prometheus LLM metrics (by caller, role and model; no ids as labels).

Routing itself stays as it is (role -> model in ``llm.models``); this only makes it
visible: the role and the model it resolved to are on every span and metric.
"""

from __future__ import annotations

import time

from aiops.core.config import ModelRole, Settings
from aiops.llm.base import (
    ChatMessage,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    ToolChoice,
    ToolSpec,
)
from aiops.observability import metrics
from aiops.observability.budget import current_scope
from aiops.observability.pricing import priced
from aiops.observability.tracing import Attributes, mark, set_attributes, span, usage_attributes


class InstrumentedLLM:
    def __init__(self, inner: LLMProvider, settings: Settings) -> None:
        self.inner = inner
        self.settings = settings

    @property
    def name(self) -> str:
        return self.inner.name

    def configured_model(self, role: ModelRole) -> str:
        """The model the profile routes ``role`` to ('fake'/'unset' when there is none)."""
        if self.inner.name == "fake":
            return "fake"
        return self.settings.llm.models_by_role().get(role, "-") or "unset"

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice = "auto",
        role: ModelRole = "agent",
        max_tokens: int = 2048,
    ) -> LLMResponse:
        scope = current_scope()
        model = self.configured_model(role)
        attributes: Attributes = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": self.inner.name,
            "gen_ai.request.model": model,
            "gen_ai.request.max_tokens": max_tokens,
            "aiops.llm.role": role,
            "aiops.agent": scope.agent,
            "aiops.llm.tools": len(tools or []),
        }
        started = time.perf_counter()
        with span(f"chat {model}", attributes) as current:
            try:
                response = await self.inner.generate(
                    messages, tools=tools, tool_choice=tool_choice, role=role, max_tokens=max_tokens
                )
            except Exception as exc:
                status = "rate_limited" if isinstance(exc, LLMRateLimitError) else "error"
                metrics.record_llm_call(
                    agent=scope.agent,
                    role=role,
                    model=model,
                    status=status,
                    duration_s=time.perf_counter() - started,
                )
                mark(current, "error", f"{type(exc).__name__}: {exc}")
                raise
            answered_by = response.model or model
            usage = priced(self.settings, answered_by, response.usage)
            if scope.budget is not None:
                scope.budget.record(scope.agent, usage)
            metrics.record_llm_call(
                agent=scope.agent,
                role=role,
                model=model,
                status="ok",
                duration_s=time.perf_counter() - started,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cost_usd=usage.cost_usd,
            )
            set_attributes(
                current,
                {
                    "gen_ai.response.model": answered_by,
                    "gen_ai.response.finish_reasons": response.finish_reason,
                    "aiops.llm.tool_calls": len(response.tool_calls),
                    **usage_attributes(usage),
                },
            )
            mark(current, "ok")
        return response.model_copy(update={"usage": usage})


def instrument(llm: LLMProvider, settings: Settings) -> LLMProvider:
    """``llm`` wrapped once (wrapping an already instrumented provider is a no-op)."""
    if isinstance(llm, InstrumentedLLM):
        return llm
    return InstrumentedLLM(llm, settings)
