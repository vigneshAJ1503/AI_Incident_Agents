"""Helpers shared by the provider adapters (not part of the public LLM API)."""

from __future__ import annotations

import json
from typing import Any

from pydantic import SecretStr

from aiops.llm.base import LLMToolCall

MASK = "********"


def mask(text: str, *secrets: SecretStr | str | None) -> str:
    """``text`` with every secret value replaced: errors and logs never show keys."""
    for secret in secrets:
        value = secret.get_secret_value() if isinstance(secret, SecretStr) else secret
        if value and len(value) >= 4:
            text = text.replace(value, MASK)
    return text


def tool_call_from_input(call_id: str, name: str, payload: Any) -> LLMToolCall:
    """A tool call whose arguments arrive already parsed (Anthropic ``input``, Bedrock
    ``toolUse.input``). A non-object payload becomes a ``parse_error``, like bad JSON."""
    if payload is None:
        payload = {}
    raw = json.dumps(payload)
    if not isinstance(payload, dict):
        return LLMToolCall(
            id=call_id, name=name, raw_arguments=raw, parse_error="arguments must be an object"
        )
    return LLMToolCall(id=call_id, name=name, arguments=payload, raw_arguments=raw)


def merge_turns(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop empty turns and merge consecutive same-role turns (``content`` block lists):
    Anthropic and Bedrock want all tool results of one assistant turn in one user turn."""
    merged: list[dict[str, Any]] = []
    for turn in turns:
        if not turn["content"]:
            continue
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"] = [*merged[-1]["content"], *turn["content"]]
        else:
            merged.append({"role": turn["role"], "content": list(turn["content"])})
    return merged
