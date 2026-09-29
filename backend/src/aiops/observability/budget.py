"""The live token/cost ledger of ONE investigation, and who is calling the LLM right now.

``Investigation.usage`` is only summed when an agent finishes, so with four agents in
parallel it lags behind what is really being spent. The ledger is updated on every LLM
call (by ``observability.llm.InstrumentedLLM``), so the orchestrator and the agents can
stop spending as soon as ``orchestrator.max_tokens`` / ``max_cost_usd`` is reached.

``llm_scope`` names the caller (``planner``, ``rca``, an agent) and its ledger in a
context variable: asyncio tasks inherit it, so each agent step sees its own scope.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from aiops.core.models import TokenUsage


@dataclass
class InvestigationBudget:
    """Spend so far vs the investigation's limits (0 cost limit = no cost cap)."""

    max_tokens: int
    max_cost_usd: float = 0.0
    spent: TokenUsage = field(default_factory=TokenUsage)
    by_agent: dict[str, TokenUsage] = field(default_factory=dict)

    def record(self, agent: str, usage: TokenUsage) -> None:
        self.spent = self.spent + usage
        self.by_agent[agent] = self.by_agent.get(agent, TokenUsage()) + usage

    @property
    def exhausted(self) -> bool:
        if self.spent.total_tokens >= self.max_tokens:
            return True
        return self.max_cost_usd > 0 and self.spent.cost_usd >= self.max_cost_usd

    def describe(self) -> str:
        """Which limit was reached, for notes and events."""
        if self.max_cost_usd > 0 and self.spent.cost_usd >= self.max_cost_usd:
            return f"cost budget (${self.max_cost_usd:g})"
        return f"token budget ({self.max_tokens})"


@dataclass(frozen=True)
class LLMScope:
    agent: str
    budget: InvestigationBudget | None = None


_SCOPE: ContextVar[LLMScope | None] = ContextVar("aiops_llm_scope", default=None)
DEFAULT_CALLER = "orchestrator"


def current_scope() -> LLMScope:
    return _SCOPE.get() or LLMScope(DEFAULT_CALLER)


@contextmanager
def llm_scope(agent: str, budget: InvestigationBudget | None = None) -> Iterator[LLMScope]:
    """LLM calls made inside are attributed to ``agent`` (metrics, spans, the ledger)."""
    scope = LLMScope(agent, budget if budget is not None else current_scope().budget)
    token = _SCOPE.set(scope)
    try:
        yield scope
    finally:
        _SCOPE.reset(token)
