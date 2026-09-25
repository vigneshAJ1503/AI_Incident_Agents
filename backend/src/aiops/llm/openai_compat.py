"""OpenAI-compatible chat completions provider.

Works with hosted free tiers that expose the OpenAI API shape — Groq
(https://api.groq.com/openai/v1) and Google Gemini
(https://generativelanguage.googleapis.com/v1beta/openai/). No local models.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx2
import openai
from openai import AsyncOpenAI

from aiops.core.config import ConfigError, LLMConfig, ModelRole
from aiops.core.models import TokenUsage
from aiops.llm.base import (
    ChatMessage,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    LLMToolCall,
    ToolChoice,
    ToolSpec,
)

log = logging.getLogger(__name__)


class OpenAICompatProvider:
    def __init__(self, config: LLMConfig, http_client: httpx2.AsyncClient | None = None) -> None:
        if not config.base_url:
            raise ConfigError("LLM base_url is not set (OPENAI_COMPAT_BASE_URL).")
        if config.api_key is None:
            raise ConfigError(
                "LLM API key is not set (OPENAI_COMPAT_API_KEY). "
                "Get a free key: see docs/setup/zero-cost.md."
            )
        self._config = config
        # The SDK retries 408/409/429/5xx with exponential backoff (honours Retry-After).
        self._client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key.get_secret_value(),
            timeout=config.timeout_s,
            max_retries=config.max_retries,
            http_client=http_client,
        )

    @property
    def name(self) -> str:
        return "openai_compat"

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
        request: dict[str, Any] = {
            "model": model,
            "messages": [_to_wire(m) for m in messages],
            "temperature": self._config.temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            request["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]
            request["tool_choice"] = tool_choice
        try:
            completion = await self._client.chat.completions.create(**request)
        except openai.RateLimitError as exc:
            raise LLMRateLimitError(f"Rate limited by provider after retries: {exc}") from exc
        except openai.APIStatusError as exc:
            raise LLMError(f"Provider returned HTTP {exc.status_code}: {exc.message}") from exc
        except openai.APIError as exc:  # connection errors, timeouts
            raise LLMError(f"Provider request failed: {exc}") from exc

        if not completion.choices:
            raise LLMError("Provider returned no choices.")
        choice = completion.choices[0]
        usage = completion.usage
        return LLMResponse(
            content=choice.message.content,
            tool_calls=[_parse_tool_call(tc) for tc in choice.message.tool_calls or []],
            usage=TokenUsage(
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                calls=1,
            ),
            model=completion.model or model,
            finish_reason=choice.finish_reason,
        )


def _to_wire(message: ChatMessage) -> dict[str, Any]:
    wire: dict[str, Any] = {"role": message.role, "content": message.content}
    if message.role == "tool":
        wire["tool_call_id"] = message.tool_call_id
    if message.tool_calls:
        wire["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": tc.raw_arguments or json.dumps(tc.arguments),
                },
            }
            for tc in message.tool_calls
        ]
    return wire


def _parse_tool_call(tool_call: Any) -> LLMToolCall:
    raw = tool_call.function.arguments or "{}"
    try:
        arguments = json.loads(raw)
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be a JSON object")
        return LLMToolCall(
            id=tool_call.id, name=tool_call.function.name, arguments=arguments, raw_arguments=raw
        )
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("Unparseable tool arguments for %s: %s", tool_call.function.name, exc)
        return LLMToolCall(
            id=tool_call.id, name=tool_call.function.name, raw_arguments=raw, parse_error=str(exc)
        )
