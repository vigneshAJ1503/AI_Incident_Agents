"""Build the configured LLM provider."""

from __future__ import annotations

from aiops.core.config import LLMConfig
from aiops.llm.base import LLMProvider
from aiops.llm.fake import FakeLLMProvider
from aiops.llm.openai_compat import OpenAICompatProvider


def create_provider(config: LLMConfig) -> LLMProvider:
    if config.provider == "openai_compat":
        return OpenAICompatProvider(config)
    return FakeLLMProvider()
