from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel, SecretStr

from aiops.core.config import ConfigError, LLMConfig
from aiops.llm.base import ChatMessage, LLMError, LLMRateLimitError, ToolSpec
from aiops.llm.fake import FakeLLMProvider, text, tool_call
from aiops.llm.openai_compat import OpenAICompatProvider
from aiops.llm.structured import generate_structured

CONFIG = LLMConfig(
    provider="openai_compat",
    base_url="https://llm.test/v1",
    api_key=SecretStr("test-key"),
    models={"fast": "small-model", "agent": "big-model"},
    max_retries=0,
)


def completion(message: dict[str, Any], model: str = "big-model") -> dict[str, Any]:
    return {
        "id": "cmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


def make_provider(handler: Any) -> tuple[OpenAICompatProvider, list[dict[str, Any]]]:
    seen: list[dict[str, Any]] = []

    def _handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        seen.append(
            {"body": body, "auth": request.headers.get("authorization"), "url": str(request.url)}
        )
        return handler(body)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(_handle))
    return OpenAICompatProvider(CONFIG, http_client=client), seen


async def test_text_completion_uses_role_model_and_reports_usage() -> None:
    provider, seen = make_provider(
        lambda body: httpx2.Response(
            200, json=completion({"role": "assistant", "content": "pong"}, body["model"])
        )
    )
    response = await provider.generate([ChatMessage.user("ping")], role="fast")
    assert response.content == "pong"
    assert response.usage.input_tokens == 12 and response.usage.calls == 1
    assert seen[0]["body"]["model"] == "small-model"
    assert seen[0]["auth"] == "Bearer test-key"
    assert seen[0]["url"] == "https://llm.test/v1/chat/completions"
    assert "tools" not in seen[0]["body"]


async def test_tool_calls_round_trip() -> None:
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "search_logs", "arguments": '{"level": "ERROR"}'},
            },
            {
                "id": "c2",
                "type": "function",
                "function": {"name": "search_logs", "arguments": "{not json"},
            },
        ],
    }
    provider, seen = make_provider(lambda body: httpx2.Response(200, json=completion(message)))
    tools = [ToolSpec(name="search_logs", description="Search logs")]
    response = await provider.generate([ChatMessage.user("q")], tools=tools, tool_choice="required")
    assert response.tool_calls[0].arguments == {"level": "ERROR"}
    assert response.tool_calls[1].parse_error is not None
    body = seen[0]["body"]
    assert body["tool_choice"] == "required"
    assert body["tools"][0]["function"]["name"] == "search_logs"

    # Assistant tool calls + tool results serialize back to the wire format.
    history = [
        ChatMessage.user("q"),
        response.as_message(),
        ChatMessage.tool_result("c1", "3 hits"),
    ]
    await provider.generate(history)
    wire = seen[1]["body"]["messages"]
    assert wire[1]["tool_calls"][0]["function"]["arguments"] == '{"level": "ERROR"}'
    assert wire[2] == {"role": "tool", "content": "3 hits", "tool_call_id": "c1"}


async def test_rate_limit_maps_to_rate_limit_error() -> None:
    provider, _ = make_provider(
        lambda body: httpx2.Response(429, json={"error": {"message": "slow down"}})
    )
    with pytest.raises(LLMRateLimitError):
        await provider.generate([ChatMessage.user("q")])


async def test_bad_request_maps_to_llm_error() -> None:
    provider, _ = make_provider(
        lambda body: httpx2.Response(400, json={"error": {"message": "bad tool"}})
    )
    with pytest.raises(LLMError, match="400"):
        await provider.generate([ChatMessage.user("q")])


def test_missing_api_key_is_config_error() -> None:
    with pytest.raises(ConfigError, match="zero-cost"):
        OpenAICompatProvider(CONFIG.model_copy(update={"api_key": None}))


class Verdict(BaseModel):
    service: str
    confidence: float


async def test_structured_output_success() -> None:
    fake = FakeLLMProvider([tool_call("submit", {"service": "payment-service", "confidence": 0.9})])
    result, usage = await generate_structured(fake, [ChatMessage.user("q")], Verdict)
    assert result == Verdict(service="payment-service", confidence=0.9)
    assert usage.calls == 1
    assert fake.requests[0]["tool_choice"] == "required"
    assert fake.requests[0]["tools"][0].parameters["properties"]["service"]["type"] == "string"


async def test_structured_output_retries_once_with_feedback() -> None:
    fake = FakeLLMProvider(
        [
            tool_call("submit", {"service": "payment-service"}),
            tool_call("submit", {"service": "x", "confidence": 0.1}),
        ]
    )
    result, usage = await generate_structured(fake, [ChatMessage.user("q")], Verdict)
    assert result.service == "x"
    assert usage.calls == 2
    retry_messages = fake.requests[1]["messages"]
    assert retry_messages[-1].role == "user" and "rejected" in (retry_messages[-1].content or "")
    assert retry_messages[-2].role == "tool"


async def test_structured_output_gives_up() -> None:
    fake = FakeLLMProvider([text("no tool"), text("still no tool")])
    with pytest.raises(LLMError, match="did not call"):
        await generate_structured(fake, [ChatMessage.user("q")], Verdict)


async def test_fake_script_exhausted() -> None:
    with pytest.raises(LLMError, match="exhausted"):
        await FakeLLMProvider().generate([ChatMessage.user("q")])
