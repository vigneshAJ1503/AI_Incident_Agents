"""OpenAI-compatible Chat Completions provider.

Any hosted endpoint that exposes the OpenAI API shape with tool calling, selected by
``llm.base_url`` (docs/setup/llm-providers.md):

- Groq (free tier): ``https://api.groq.com/openai/v1``
- Google Gemini (free tier): ``https://generativelanguage.googleapis.com/v1beta/openai/``
- OpenRouter: ``https://openrouter.ai/api/v1``
- OpenAI: ``https://api.openai.com/v1``
- a company-hosted vLLM / LiteLLM / Ollama *server*: ``https://llm.internal.example/v1``

Never a model on the developer's laptop (project rule): a company's own inference server
is fine. Azure OpenAI has its own adapter (``azure_openai``) that reuses this one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx2
import openai
from openai import AsyncOpenAI

from aiops.core.config import ConfigError, LLMConfig, ModelRole
from aiops.core.models import TokenUsage
from aiops.llm._common import mask
from aiops.llm.base import (
    ChatMessage,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    LLMToolCall,
    LLMToolCallError,
    ToolChoice,
    ToolSpec,
)

log = logging.getLogger(__name__)


class OpenAICompatProvider:
    def __init__(self, config: LLMConfig, http_client: httpx2.AsyncClient | None = None) -> None:
        self._config = config
        self._require(config)
        # The SDK retries 408/409/429/5xx with exponential backoff (honours Retry-After).
        # With fallback keys (free tiers cap tokens per minute PER KEY), retry each key only
        # briefly and rotate to the next key on a rate limit instead of sleeping.
        keys = [config.api_key, *config.fallback_api_keys]
        retries = config.max_retries if len(keys) == 1 else min(config.max_retries, 1)
        self._clients = [
            self._build_client(
                config.model_copy(update={"api_key": key, "max_retries": retries}), http_client
            )
            for key in keys
        ]
        self._client = self._clients[0]
        self._next = 0  # round-robin start, so load spreads across keys
        limit = config.max_concurrent_requests
        self._gate = asyncio.Semaphore(limit) if limit else None

    def _require(self, config: LLMConfig) -> None:
        if not config.base_url:
            raise ConfigError("LLM base_url is not set (OPENAI_COMPAT_BASE_URL).")
        if config.api_key is None:
            raise ConfigError(
                "LLM API key is not set (OPENAI_COMPAT_API_KEY). "
                "Get a free key: see docs/setup/zero-cost.md."
            )

    def _build_client(
        self, config: LLMConfig, http_client: httpx2.AsyncClient | None
    ) -> AsyncOpenAI:
        assert config.api_key is not None  # noqa: S101 (checked in _require)
        return AsyncOpenAI(
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
        if self._config.extra:
            request["extra_body"] = dict(self._config.extra)
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
            forced = tool_choice == "required" and not self._config.forced_tool_choice
            request["tool_choice"] = "auto" if forced else tool_choice
        secret = self._config.api_key
        try:
            completion = await self._create(request)
        except openai.RateLimitError as exc:
            raise LLMRateLimitError(
                mask(f"{self.name}: rate limited after retries: {exc}", secret)
            ) from None
        except openai.APIStatusError as exc:
            if exc.status_code == 400 and _is_tool_call_rejection(exc):
                # Recoverable: the agent feeds this back to the model (schema mismatch).
                raise LLMToolCallError(mask(_rejection_message(exc), secret)) from None
            raise LLMError(
                mask(f"{self.name}: HTTP {exc.status_code}: {exc.message}", secret)
            ) from None
        except openai.APIError as exc:  # connection errors, timeouts
            raise LLMError(mask(f"{self.name}: request failed: {exc}", secret)) from None

        if not completion.choices:
            raise LLMError(f"{self.name}: the provider returned no choices.")
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

    async def _create(self, request: dict[str, Any]) -> Any:
        """One completion, rotating to the next API key when a key is rate-limited."""
        if self._gate is None:
            return await self._rotate(request)
        async with self._gate:
            return await self._rotate(request)

    async def _rotate(self, request: dict[str, Any]) -> Any:
        order = [(self._next + i) % len(self._clients) for i in range(len(self._clients))]
        self._next = (self._next + 1) % len(self._clients)
        last: openai.RateLimitError | None = None
        for index in order:
            try:
                return await self._clients[index].chat.completions.create(**request)
            except openai.RateLimitError as exc:
                log.warning("LLM key #%d rate-limited; trying the next key", index + 1)
                last = exc
        assert last is not None  # noqa: S101 - at least one client exists
        raise last


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


def _error_body(exc: openai.APIStatusError) -> dict[str, Any]:
    body = exc.body if isinstance(exc.body, dict) else {}
    inner = body.get("error", body)
    return inner if isinstance(inner, dict) else {}


def _is_tool_call_rejection(exc: openai.APIStatusError) -> bool:
    """Groq/OpenAI-compatible servers validate tool arguments server-side ("tool_use_failed")."""
    error = _error_body(exc)
    code = str(error.get("code") or error.get("type") or "")
    message = str(error.get("message", ""))
    # "Parsing failed. The model generated output that could not be parsed" is the same
    # class of problem: the model's tool call was unusable, so let it try again.
    return code == "tool_use_failed" or any(
        marker in message for marker in ("tool call validation failed", "Parsing failed")
    )


def _rejection_message(exc: openai.APIStatusError) -> str:
    message = str(_error_body(exc).get("message") or exc.message)
    return message[:1500]
