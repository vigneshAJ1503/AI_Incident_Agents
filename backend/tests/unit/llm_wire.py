"""Mocked vendor transports for the LLM adapters (zero tokens, no network, no keys).

Each ``*_server`` turns a provider-neutral responder (``messages, tools -> LLMResponse``,
e.g. ``echo_responder``) into that vendor's wire protocol, so the real adapter code
(SDK request building, response parsing, retries, error mapping) runs end to end:

- Anthropic Messages API and Azure OpenAI: an ``httpx2.MockTransport`` handed to the SDK
- Amazon Bedrock Converse: a botocore ``before-send`` hook that answers every HTTP request
  after signing and before the network (botocore's retry handler still runs)
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import boto3
import httpx2
from botocore.awsrequest import AWSResponse
from botocore.config import Config
from pydantic import SecretStr

from aiops.core.config import LLMConfig
from aiops.llm.anthropic_messages import AnthropicProvider
from aiops.llm.azure_openai import AzureOpenAIProvider
from aiops.llm.base import ChatMessage, LLMResponse, LLMToolCall, ToolSpec
from aiops.llm.bedrock import BedrockProvider

Responder = Callable[[list[ChatMessage], list[ToolSpec] | None], LLMResponse]
#: (status, json body, headers) for a request, or None to use the responder.
Override = Callable[[int], tuple[int, dict[str, Any], dict[str, str]] | None]

#: Built at runtime: the repo is public and gitleaks runs in CI.
ANTHROPIC_KEY = "-".join(["sk", "ant", "fake", uuid4().hex])
AZURE_KEY = "-".join(["azure", "fake", uuid4().hex])
AWS_ACCESS_KEY = "AKIA" + uuid4().hex[:16].upper()
AWS_SECRET_KEY = "-".join(["aws", "secret", uuid4().hex])

MODELS = {"fast": "fast-model", "agent": "agent-model", "rca": "rca-model"}
USAGE = (7, 3)  # input, output tokens per scripted response


class Recorder:
    """What the fake server saw: one dict per HTTP request (the decoded body + headers)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    @property
    def bodies(self) -> list[dict[str, Any]]:
        return [r["body"] for r in self.requests]


# --------------------------------------------------------------------------- Anthropic


def anthropic_decode(body: dict[str, Any]) -> tuple[list[ChatMessage], list[ToolSpec] | None]:
    messages = [ChatMessage.system(body["system"])] if body.get("system") else []
    for turn in body["messages"]:
        content = turn["content"]
        if isinstance(content, str):
            messages.append(ChatMessage(role=turn["role"], content=content))
            continue
        texts = [b["text"] for b in content if b["type"] == "text"]
        calls = [
            LLMToolCall(id=b["id"], name=b["name"], arguments=b["input"])
            for b in content
            if b["type"] == "tool_use"
        ]
        for b in content:
            if b["type"] == "tool_result":
                messages.append(ChatMessage.tool_result(b["tool_use_id"], b["content"]))
        if texts or calls:
            messages.append(
                ChatMessage(role=turn["role"], content="".join(texts) or None, tool_calls=calls)
            )
    tools = [
        ToolSpec(name=t["name"], description=t["description"], parameters=t["input_schema"])
        for t in body.get("tools", [])
    ]
    return messages, tools or None


def anthropic_encode(response: LLMResponse, model: str) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if response.content:
        content.append({"type": "text", "text": response.content})
    for call in response.tool_calls:
        content.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return {
        "id": f"msg_{uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "tool_use" if response.tool_calls else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": USAGE[0],
            "output_tokens": USAGE[1],
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def anthropic_error(status: int, kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


def anthropic_config(**update: Any) -> LLMConfig:
    config = LLMConfig(
        provider="anthropic",
        api_key=SecretStr(ANTHROPIC_KEY),
        models=dict(MODELS),  # type: ignore[arg-type]
        max_retries=0,
    )
    return config.model_copy(update=update)


def anthropic_server(
    responder: Responder, override: Override | None = None, config: LLMConfig | None = None
) -> tuple[AnthropicProvider, Recorder]:
    recorder = Recorder()

    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        recorder.requests.append(
            {"body": body, "headers": dict(request.headers), "url": str(request.url)}
        )
        if override and (answer := override(len(recorder.requests))):
            status, payload, headers = answer
            return httpx2.Response(status, json=payload, headers=headers)
        messages, tools = anthropic_decode(body)
        return httpx2.Response(
            200, json=anthropic_encode(responder(messages, tools), body["model"])
        )

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    return AnthropicProvider(config or anthropic_config(), http_client=client), recorder


# --------------------------------------------------------------------------- Azure OpenAI


def openai_decode(body: dict[str, Any]) -> tuple[list[ChatMessage], list[ToolSpec] | None]:
    messages = []
    for m in body["messages"]:
        calls = [
            LLMToolCall(
                id=c["id"],
                name=c["function"]["name"],
                arguments=json.loads(c["function"]["arguments"]),
            )
            for c in m.get("tool_calls") or []
        ]
        messages.append(
            ChatMessage(
                role=m["role"],
                content=m.get("content"),
                tool_calls=calls,
                tool_call_id=m.get("tool_call_id"),
            )
        )
    tools = [
        ToolSpec(
            name=t["function"]["name"],
            description=t["function"]["description"],
            parameters=t["function"]["parameters"],
        )
        for t in body.get("tools", [])
    ]
    return messages, tools or None


def openai_encode(response: LLMResponse, model: str) -> dict[str, Any]:
    calls = [
        {
            "id": c.id,
            "type": "function",
            "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
        }
        for c in response.tool_calls
    ]
    message: dict[str, Any] = {"role": "assistant", "content": response.content}
    if calls:
        message["tool_calls"] = calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": USAGE[0],
            "completion_tokens": USAGE[1],
            "total_tokens": sum(USAGE),
        },
    }


def azure_config(**update: Any) -> LLMConfig:
    config = LLMConfig(
        provider="azure_openai",
        base_url="https://acme-ai.openai.azure.com",
        api_key=SecretStr(AZURE_KEY),
        api_version="2024-10-21",
        models=dict(MODELS),  # type: ignore[arg-type]
        max_retries=0,
    )
    return config.model_copy(update=update)


def azure_server(
    responder: Responder, override: Override | None = None, config: LLMConfig | None = None
) -> tuple[AzureOpenAIProvider, Recorder]:
    recorder = Recorder()

    def handle(request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        recorder.requests.append(
            {"body": body, "headers": dict(request.headers), "url": str(request.url)}
        )
        if override and (answer := override(len(recorder.requests))):
            status, payload, headers = answer
            return httpx2.Response(status, json=payload, headers=headers)
        messages, tools = openai_decode(body)
        deployment = str(request.url.path).split("/deployments/")[1].split("/")[0]
        return httpx2.Response(200, json=openai_encode(responder(messages, tools), deployment))

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    return AzureOpenAIProvider(config or azure_config(), http_client=client), recorder


# --------------------------------------------------------------------------- Bedrock


def bedrock_decode(body: dict[str, Any]) -> tuple[list[ChatMessage], list[ToolSpec] | None]:
    messages = [ChatMessage.system(s["text"]) for s in body.get("system", [])]
    for turn in body["messages"]:
        texts = [b["text"] for b in turn["content"] if "text" in b]
        calls = [
            LLMToolCall(
                id=b["toolUse"]["toolUseId"],
                name=b["toolUse"]["name"],
                arguments=b["toolUse"]["input"],
            )
            for b in turn["content"]
            if "toolUse" in b
        ]
        for b in turn["content"]:
            if "toolResult" in b:
                result = b["toolResult"]
                text = "".join(c.get("text", "") for c in result["content"])
                messages.append(ChatMessage.tool_result(result["toolUseId"], text))
        if texts or calls:
            messages.append(
                ChatMessage(role=turn["role"], content="".join(texts) or None, tool_calls=calls)
            )
    tools = [
        ToolSpec(
            name=t["toolSpec"]["name"],
            description=t["toolSpec"]["description"],
            parameters=t["toolSpec"]["inputSchema"]["json"],
        )
        for t in body.get("toolConfig", {}).get("tools", [])
    ]
    return messages, tools or None


def bedrock_encode(response: LLMResponse) -> dict[str, Any]:
    content: list[dict[str, Any]] = []
    if response.content:
        content.append({"text": response.content})
    for call in response.tool_calls:
        content.append(
            {"toolUse": {"toolUseId": call.id, "name": call.name, "input": call.arguments}}
        )
    return {
        "output": {"message": {"role": "assistant", "content": content}},
        "stopReason": "tool_use" if response.tool_calls else "end_turn",
        "usage": {
            "inputTokens": USAGE[0],
            "outputTokens": USAGE[1],
            "totalTokens": sum(USAGE),
        },
        "metrics": {"latencyMs": 1},
    }


class _Raw:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **_: Any) -> Any:
        yield self._body


def bedrock_config(**update: Any) -> LLMConfig:
    config = LLMConfig(provider="bedrock", models=dict(MODELS), max_retries=0)  # type: ignore[arg-type]
    return config.model_copy(update=update)


def bedrock_server(
    responder: Responder,
    override: Override | None = None,
    config: LLMConfig | None = None,
    max_attempts: int = 1,
) -> tuple[BedrockProvider, Recorder]:
    """A real boto3 ``bedrock-runtime`` client (fake credentials built at runtime) whose
    HTTP layer is replaced by the responder."""
    recorder = Recorder()
    session = boto3.session.Session(
        aws_access_key_id=AWS_ACCESS_KEY,
        aws_secret_access_key=AWS_SECRET_KEY,
        region_name="us-east-1",
    )
    client = session.client(
        "bedrock-runtime",
        config=Config(retries={"total_max_attempts": max_attempts, "mode": "standard"}),
    )

    def handle(request: Any, **_: Any) -> AWSResponse:
        body = json.loads(request.body)
        recorder.requests.append(
            {"body": body, "headers": dict(request.headers), "url": request.url}
        )
        if override and (answer := override(len(recorder.requests))):
            status, payload, headers = answer
        else:
            messages, tools = bedrock_decode(body)
            status, payload, headers = 200, bedrock_encode(responder(messages, tools)), {}
        headers = {"content-type": "application/json", **headers}
        return AWSResponse(request.url, status, headers, _Raw(json.dumps(payload).encode()))

    client.meta.events.register("before-send.bedrock-runtime.Converse", handle)
    return BedrockProvider(config or bedrock_config(), client=client), recorder


def no_botocore_sleep() -> types.SimpleNamespace:
    """Replacement for ``botocore.endpoint.time``: retries without waiting."""
    return types.SimpleNamespace(sleep=lambda _seconds: None)
