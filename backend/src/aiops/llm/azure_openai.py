"""Azure OpenAI provider: the OpenAI adapter on ``AsyncAzureOpenAI``.

``llm.base_url`` is the resource endpoint (``https://<resource>.openai.azure.com``),
``llm.api_version`` the ``api-version`` query parameter, and ``llm.models`` map each role
to a *deployment* name (Azure routes by deployment, not by model id). Request/response
mapping, tool calling, retries and error handling are the OpenAI adapter's.
"""

from __future__ import annotations

import httpx2
from openai import AsyncAzureOpenAI, AsyncOpenAI

from aiops.core.config import ConfigError, LLMConfig
from aiops.llm.openai_compat import OpenAICompatProvider


class AzureOpenAIProvider(OpenAICompatProvider):
    def _require(self, config: LLMConfig) -> None:
        problems = [p for p in config.missing() if not p.startswith("llm.models")]
        if problems:
            raise ConfigError(
                "Azure OpenAI is not configured: "
                + "; ".join(problems)
                + ". See docs/setup/llm-providers.md#azure-openai."
            )

    def _build_client(
        self, config: LLMConfig, http_client: httpx2.AsyncClient | None
    ) -> AsyncOpenAI:
        assert config.api_key is not None and config.base_url  # noqa: S101 (_require)
        return AsyncAzureOpenAI(
            azure_endpoint=config.base_url,
            api_key=config.api_key.get_secret_value(),
            api_version=config.api_version,
            timeout=config.timeout_s,
            max_retries=config.max_retries,
            http_client=http_client,
        )

    @property
    def name(self) -> str:
        return "azure_openai"
