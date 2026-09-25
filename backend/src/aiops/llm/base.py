"""Provider-neutral chat + tool-calling types.

The rest of the codebase depends only on these types, never on a vendor SDK.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from aiops.core.config import ModelRole
from aiops.core.models import TokenUsage

ToolChoice = Literal["auto", "required", "none"]


class LLMError(Exception):
    """The provider failed (bad request, auth, server error, unparseable output)."""


class LLMRateLimitError(LLMError):
    """Rate limit still exceeded after retries (common on free tiers)."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolSpec(_Model):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class LLMToolCall(_Model):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    raw_arguments: str = ""
    parse_error: str | None = None


class ChatMessage(_Model):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # for role == "tool"

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str) -> ChatMessage:
        return cls(role="tool", tool_call_id=tool_call_id, content=content)


class LLMResponse(_Model):
    content: str | None = None
    tool_calls: list[LLMToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    model: str = ""
    finish_reason: str | None = None

    def as_message(self) -> ChatMessage:
        return ChatMessage(role="assistant", content=self.content, tool_calls=self.tool_calls)


@runtime_checkable
class LLMProvider(Protocol):
    @property
    def name(self) -> str: ...

    async def generate(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec] | None = None,
        tool_choice: ToolChoice = "auto",
        role: ModelRole = "agent",
        max_tokens: int = 2048,
    ) -> LLMResponse: ...
