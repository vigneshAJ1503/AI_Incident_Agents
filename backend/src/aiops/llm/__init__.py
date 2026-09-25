"""LLM provider abstraction. No model ever runs locally: hosted free tiers or a fake."""

from aiops.llm.base import (
    ChatMessage,
    LLMError,
    LLMProvider,
    LLMRateLimitError,
    LLMResponse,
    LLMToolCall,
    ToolSpec,
)

__all__ = [
    "ChatMessage",
    "LLMError",
    "LLMProvider",
    "LLMRateLimitError",
    "LLMResponse",
    "LLMToolCall",
    "ToolSpec",
]
