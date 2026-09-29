"""A chain of LLM providers: the next one answers when one is rate-limited or down.

Free tiers cap tokens per minute and per day, per provider. With ``llm.fallbacks`` in the
profile (e.g. Groq first, Gemini's free tier second), an investigation keeps reasoning
with a real model instead of dropping to the deterministic analysis when the first
provider's quota runs out. Each provider keeps its own models, keys and settings.

Only availability errors move to the next provider: a rejected tool call
(``LLMToolCallError``) goes back to the agent, which corrects the SAME model.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from aiops.core.config import ModelRole
from aiops.llm.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMToolCallError,
    ToolChoice,
    ToolSpec,
)

log = logging.getLogger(__name__)

#: How long a rate-limited provider is skipped before it is tried again.
COOLDOWN_S = 30.0


class FallbackLLMProvider:
    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        cooldown_s: float = COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not providers:
            raise ValueError("FallbackLLMProvider needs at least one provider")
        self._providers = providers
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._resting_until = [0.0] * len(providers)

    @property
    def name(self) -> str:
        return self._providers[0].name

    @property
    def providers(self) -> list[LLMProvider]:
        return list(self._providers)

    def _order(self) -> list[int]:
        """Providers not cooling down first (in profile order), then the resting ones:
        when every provider is rate-limited, still try rather than fail without a call."""
        now = self._clock()
        ready = [i for i, until in enumerate(self._resting_until) if until <= now]
        resting = [i for i in range(len(self._providers)) if i not in ready]
        return ready + resting

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice = "auto",
        role: ModelRole = "agent",
        max_tokens: int = 2048,
    ) -> LLMResponse:
        last: LLMError | None = None
        for index in self._order():
            provider = self._providers[index]
            try:
                return await provider.generate(
                    messages, tools=tools, tool_choice=tool_choice, role=role, max_tokens=max_tokens
                )
            except LLMToolCallError:
                raise  # the model's own mistake: the agent corrects the same model
            except LLMRateLimitError as exc:
                self._resting_until[index] = self._clock() + self._cooldown_s
                log.warning(
                    "LLM provider #%d (%s) rate-limited; trying the next one",
                    index + 1,
                    provider.name,
                )
                last = exc
            except LLMError as exc:
                log.warning(
                    "LLM provider #%d (%s) failed; trying the next one", index + 1, provider.name
                )
                last = exc
        assert last is not None  # noqa: S101 - at least one provider was tried
        raise last
