"""Build the configured LLM provider (``llm.provider`` in the profile).

Vendor SDKs are imported lazily, so a profile only loads the SDK it uses.
"""

from __future__ import annotations

import logging

from aiops.core.config import LLMConfig
from aiops.llm.base import LLMProvider
from aiops.llm.fake import FakeLLMProvider

log = logging.getLogger(__name__)


def create_provider(config: LLMConfig) -> LLMProvider:
    """The adapter for ``config.provider``; raises ``ConfigError`` (readable, names the
    missing field/env var) when the profile can't make real calls. With ``llm.fallbacks``,
    a chain that moves to the next configured provider on a rate limit or outage."""
    primary = _single(config)
    backups = []
    for i, fallback in enumerate(config.fallbacks, start=1):
        problems = config_problems(fallback)
        if problems:  # e.g. GEMINI_API_KEY not set yet: fine, the chain is just shorter
            log.info(
                "llm.fallbacks[%d] (%s) skipped: %s", i, fallback.provider, "; ".join(problems)
            )
            continue
        backups.append(_single(fallback))
    if not backups:
        return primary
    from aiops.llm.fallback import FallbackLLMProvider

    return FallbackLLMProvider([primary, *backups])


def _single(config: LLMConfig) -> LLMProvider:
    if config.provider == "openai_compat":
        from aiops.llm.openai_compat import OpenAICompatProvider

        return OpenAICompatProvider(config)
    if config.provider == "anthropic":
        from aiops.llm.anthropic_messages import AnthropicProvider

        return AnthropicProvider(config)
    if config.provider == "bedrock":
        from aiops.llm.bedrock import BedrockProvider

        return BedrockProvider(config)
    if config.provider == "azure_openai":
        from aiops.llm.azure_openai import AzureOpenAIProvider

        return AzureOpenAIProvider(config)
    return FakeLLMProvider()


def config_problems(config: LLMConfig) -> list[str]:
    """``config.missing()`` plus environment checks that need no network: for Bedrock,
    whether the AWS chain resolves a region. Empty = a real provider can be built."""
    problems = config.missing()
    if config.provider == "bedrock":
        from aiops.llm.bedrock import aws_region

        if not aws_region():
            problems.append("no AWS region (AWS_REGION, AWS_DEFAULT_REGION or AWS_PROFILE)")
    return problems


def llm_configured(config: LLMConfig) -> bool:
    """A real, hosted LLM is usable (no secret is exposed, no network call is made)."""
    return config.provider != "fake" and not config_problems(config)
