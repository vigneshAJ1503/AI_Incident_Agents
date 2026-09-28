"""Anthropic provider: Claude via the Messages API (official ``anthropic`` SDK).

Mapping to the provider-neutral types (``aiops.llm.base``):

- ``system`` messages -> the top-level ``system`` parameter (joined in order)
- assistant ``tool_calls`` -> ``tool_use`` blocks; ``tool`` messages -> ``tool_result``
  blocks, merged into one user turn per assistant turn (parallel tool use)
- ``tool_choice``: auto -> ``{"type": "auto"}``, required -> ``{"type": "any"}``,
  none -> ``{"type": "none"}`` (``llm.forced_tool_choice: false`` sends ``auto``
  instead of ``any`` for models that reject forced tool use)
- usage: input = input + cache-creation + cache-read tokens, output = output tokens
- retries: the SDK retries 408/409/429/5xx/529 (overloaded) with exponential backoff and
  honours ``retry-after``; afterwards 429 -> ``LLMRateLimitError``, others -> ``LLMError``

Model ids come from the profile's ``llm.models`` roles only; nothing is hard-coded.
"""

from __future__ import annotations

from typing import Any

import anthropic
import httpx2
from anthropic import AsyncAnthropic

from aiops.core.config import ConfigError, LLMConfig, ModelRole
from aiops.core.models import TokenUsage
from aiops.llm._common import mask, merge_turns, tool_call_from_input
from aiops.llm.base import (
    ChatMessage,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    ToolChoice,
    ToolSpec,
)

#: Anthropic stop reasons -> the OpenAI-style finish reasons the rest of the code logs.
STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


class AnthropicProvider:
    def __init__(self, config: LLMConfig, http_client: httpx2.AsyncClient | None = None) -> None:
        if config.api_key is None or not config.api_key.get_secret_value().strip():
            raise ConfigError(
                "Anthropic API key is not set (llm.api_key, usually ${ANTHROPIC_API_KEY}). "
                "See docs/setup/llm-providers.md#anthropic."
            )
        self._config = config
        # base_url=None -> the SDK default (https://api.anthropic.com); a gateway otherwise.
        self._client = AsyncAnthropic(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url or None,
            timeout=config.timeout_s,
            max_retries=config.max_retries,
            http_client=http_client,
        )

    @property
    def name(self) -> str:
        return "anthropic"

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice = "auto",
        role: ModelRole = "agent",
        max_tokens: int = 2048,
    ) -> LLMResponse:
        model = self._config.model_for(role)
        system, turns = to_wire(messages)
        request: dict[str, Any] = {"model": model, "max_tokens": max_tokens, "messages": turns}
        if system:
            request["system"] = system
        if self._config.explicit_temperature is not None:
            # SDK 1.x dropped sampling params (recent Claude models reject them); older
            # models still accept it on the wire, so an explicit profile value is passed on.
            request["extra_body"] = {"temperature": self._config.explicit_temperature}
        if tools:
            request["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
            request["tool_choice"] = self._tool_choice(tool_choice)
        secret = self._config.api_key
        try:
            message = await self._client.messages.create(**request)
        except anthropic.RateLimitError as exc:
            raise LLMRateLimitError(
                mask(f"anthropic: rate limited after retries: {exc.message}", secret)
            ) from None
        except anthropic.APIStatusError as exc:
            raise LLMError(
                mask(f"anthropic: HTTP {exc.status_code}: {exc.message}", secret)
            ) from None
        except anthropic.APIError as exc:  # connection errors, timeouts
            raise LLMError(mask(f"anthropic: request failed: {exc}", secret)) from None
        return from_wire(message, model)

    def _tool_choice(self, choice: ToolChoice) -> dict[str, str]:
        if choice == "required":
            return {"type": "any" if self._config.forced_tool_choice else "auto"}
        return {"type": choice}


def to_wire(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    """``(system, messages)`` in the Messages API shape."""
    system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
    turns: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.tool_call_id or "",
                "content": m.content or "",
            }
            turns.append({"role": "user", "content": [block]})
            continue
        blocks: list[dict[str, Any]] = []
        if m.content:
            blocks.append({"type": "text", "text": m.content})
        for call in m.tool_calls:
            blocks.append(
                {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
            )
        turns.append({"role": m.role, "content": blocks})
    return system, merge_turns(turns)


def from_wire(message: Any, model: str) -> LLMResponse:
    texts: list[str] = []
    calls = []
    for block in message.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            calls.append(tool_call_from_input(block.id, block.name, block.input))
    usage = message.usage
    prompt = (
        usage.input_tokens
        + (usage.cache_creation_input_tokens or 0)
        + (usage.cache_read_input_tokens or 0)
    )
    return LLMResponse(
        content="".join(texts) or None,
        tool_calls=calls,
        usage=TokenUsage(input_tokens=prompt, output_tokens=usage.output_tokens, calls=1),
        model=message.model or model,
        finish_reason=STOP_REASONS.get(message.stop_reason or "", message.stop_reason),
    )
