"""Amazon Bedrock provider: the Converse API via ``boto3`` (ADR-0017).

One request shape for every Bedrock model with tool use (Claude, Amazon Nova, Llama,
Mistral, ...). Credentials and region come from the standard AWS chain (env vars,
``AWS_PROFILE``/SSO, IRSA, ECS/EC2 roles), never from the profile YAML.
``llm.base_url`` optionally points at a VPC/PrivateLink endpoint. ``llm.models`` hold
model ids or inference-profile ids/ARNs (e.g. ``us.anthropic.claude-...``).

Mapping to the provider-neutral types (``aiops.llm.base``):

- ``system`` messages -> ``system=[{"text": ...}]``
- assistant ``tool_calls`` -> ``toolUse`` blocks; ``tool`` messages -> ``toolResult``
  blocks, merged into one user turn per assistant turn
- ``tool_choice``: auto -> ``{"auto": {}}``, required -> ``{"any": {}}`` (``auto`` when
  ``llm.forced_tool_choice`` is false: not every Bedrock model supports ``any``), none ->
  no ``toolChoice`` and tools only if the history needs them (Converse has no "none")
- retries: botocore "standard" mode retries throttling and 5xx with exponential backoff
  (``max_retries`` + 1 attempts); then ThrottlingException -> ``LLMRateLimitError``
- boto3 is synchronous: calls run in a worker thread (``asyncio.to_thread``)
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

from aiops.core.config import ConfigError, LLMConfig, ModelRole
from aiops.core.models import TokenUsage
from aiops.llm._common import merge_turns, tool_call_from_input
from aiops.llm.base import (
    ChatMessage,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    ToolChoice,
    ToolSpec,
)

STOP_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}
RATE_LIMIT_CODES = {"ThrottlingException", "TooManyRequestsException"}


def aws_region(session: Any | None = None) -> str | None:
    """The region of the standard AWS chain (``AWS_REGION``, ``AWS_DEFAULT_REGION``, the
    ``AWS_PROFILE`` config), or None."""
    region = os.environ.get("AWS_REGION") or (session or boto3.session.Session()).region_name
    return str(region) if region else None


def aws_credentials_found(session: Any | None = None) -> bool:
    """Whether the standard AWS credential chain yields credentials (no API call)."""
    try:
        return (session or boto3.session.Session()).get_credentials() is not None
    except BotoCoreError:
        return False


class BedrockProvider:
    def __init__(self, config: LLMConfig, client: Any | None = None) -> None:
        self._config = config
        if client is None:
            session = boto3.session.Session()
            region = aws_region(session)
            if not region:
                raise ConfigError(
                    "Amazon Bedrock needs an AWS region: set AWS_REGION (or AWS_DEFAULT_REGION, "
                    "or a region in your AWS_PROFILE). See docs/setup/llm-providers.md#bedrock."
                )
            client = session.client(
                "bedrock-runtime",
                region_name=region,
                endpoint_url=config.base_url or None,
                config=Config(
                    retries={"total_max_attempts": config.max_retries + 1, "mode": "standard"},
                    connect_timeout=min(config.timeout_s, 10.0),
                    read_timeout=config.timeout_s,
                ),
            )
        self._client = client

    @property
    def name(self) -> str:
        return "bedrock"

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
        inference: dict[str, Any] = {"maxTokens": max_tokens}
        if self._config.explicit_temperature is not None:
            inference["temperature"] = self._config.explicit_temperature
        request: dict[str, Any] = {
            "modelId": model,
            "messages": turns,
            "inferenceConfig": inference,
        }
        if system:
            request["system"] = [{"text": system}]
        tool_config = self._tool_config(tools, tool_choice, messages)
        if tool_config:
            request["toolConfig"] = tool_config
        try:
            response = await asyncio.to_thread(self._client.converse, **request)
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = error.get("Code", "ClientError")
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", "?")
            text = f"bedrock: HTTP {status} {code}: {error.get('Message', '')}"
            if code in RATE_LIMIT_CODES or status == 429:
                raise LLMRateLimitError(f"{text} (after retries)") from None
            if code == "AccessDeniedException":
                text += " (is model access enabled for this account/region?)"
            raise LLMError(text) from None
        except NoCredentialsError:
            raise LLMError(
                "bedrock: no AWS credentials found (AWS_PROFILE/SSO, env vars, IRSA or an "
                "instance role)"
            ) from None
        except BotoCoreError as exc:  # connection errors, timeouts
            raise LLMError(f"bedrock: request failed: {exc}") from None
        return from_wire(response, model)

    def _tool_config(
        self, tools: list[ToolSpec] | None, choice: ToolChoice, messages: list[ChatMessage]
    ) -> dict[str, Any] | None:
        if not tools:
            return None
        if choice == "none" and not any(m.tool_calls or m.role == "tool" for m in messages):
            return None
        config: dict[str, Any] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": t.name,
                        "description": t.description,
                        "inputSchema": {"json": t.parameters},
                    }
                }
                for t in tools
            ]
        }
        if choice == "required" and self._config.forced_tool_choice:
            config["toolChoice"] = {"any": {}}
        elif choice != "none":
            config["toolChoice"] = {"auto": {}}
        return config


def to_wire(messages: list[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    """``(system, messages)`` in the Converse shape."""
    system = "\n\n".join(m.content for m in messages if m.role == "system" and m.content)
    turns: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            result = {
                "toolUseId": m.tool_call_id or "",
                "content": [{"text": m.content or ""}],
            }
            turns.append({"role": "user", "content": [{"toolResult": result}]})
            continue
        blocks: list[dict[str, Any]] = []
        if m.content:
            blocks.append({"text": m.content})
        for call in m.tool_calls:
            blocks.append(
                {"toolUse": {"toolUseId": call.id, "name": call.name, "input": call.arguments}}
            )
        turns.append({"role": m.role, "content": blocks})
    return system, merge_turns(turns)


def from_wire(response: dict[str, Any], model: str) -> LLMResponse:
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    texts = [b["text"] for b in blocks if "text" in b]
    calls = [
        tool_call_from_input(b["toolUse"]["toolUseId"], b["toolUse"]["name"], b["toolUse"]["input"])
        for b in blocks
        if "toolUse" in b
    ]
    usage = response.get("usage", {})
    prompt = (
        usage.get("inputTokens", 0)
        + usage.get("cacheReadInputTokens", 0)
        + usage.get("cacheWriteInputTokens", 0)
    )
    stop = response.get("stopReason")
    return LLMResponse(
        content="".join(texts) or None,
        tool_calls=calls,
        usage=TokenUsage(input_tokens=prompt, output_tokens=usage.get("outputTokens", 0), calls=1),
        model=model,
        finish_reason=STOP_REASONS.get(stop or "", stop),
    )
