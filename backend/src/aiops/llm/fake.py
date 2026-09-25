"""Deterministic, scripted LLM for tests and CI (zero tokens, no network)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from aiops.core.config import ModelRole
from aiops.core.models import TokenUsage
from aiops.llm.base import ChatMessage, LLMError, LLMResponse, LLMToolCall, ToolChoice, ToolSpec

Responder = Callable[[list[ChatMessage], list[ToolSpec] | None], LLMResponse]


def tool_call(
    name: str, arguments: dict[str, Any] | None = None, call_id: str | None = None
) -> LLMResponse:
    """Scripted response: the model calls one tool."""
    return LLMResponse(
        tool_calls=[
            LLMToolCall(id=call_id or f"call-{name}", name=name, arguments=arguments or {})
        ],
        usage=TokenUsage(input_tokens=10, output_tokens=5, calls=1),
        model="fake",
        finish_reason="tool_calls",
    )


def text(content: str) -> LLMResponse:
    """Scripted response: the model answers with text."""
    return LLMResponse(
        content=content,
        usage=TokenUsage(input_tokens=10, output_tokens=5, calls=1),
        model="fake",
        finish_reason="stop",
    )


class FakeLLMProvider:
    """Returns scripted responses in order (or via a responder) and records every request."""

    def __init__(
        self, script: list[LLMResponse] | None = None, responder: Responder | None = None
    ) -> None:
        self._script = list(script or [])
        self._responder = responder
        self.requests: list[dict[str, Any]] = []

    @property
    def name(self) -> str:
        return "fake"

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice = "auto",
        role: ModelRole = "agent",
        max_tokens: int = 2048,
    ) -> LLMResponse:
        self.requests.append(
            {"messages": list(messages), "tools": tools, "tool_choice": tool_choice, "role": role}
        )
        if self._responder is not None:
            return self._responder(messages, tools)
        if not self._script:
            raise LLMError("FakeLLMProvider script exhausted")
        return self._script.pop(0)
