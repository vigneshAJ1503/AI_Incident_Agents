"""LLM cost estimates from the profile's price table (``cost.pricing``, USD per 1M tokens).

Lookup, most specific first: the model id (``llama-3.3-70b-versatile``), the provider host
(``api.groq.com``), ``*``. Unlisted = $0: the free tiers this project uses cost nothing, and
a company overrides the prices in its own profile.
"""

from __future__ import annotations

from urllib.parse import urlparse

from aiops.core.config import ModelPrice, Settings
from aiops.core.models import TokenUsage


def provider_host(settings: Settings) -> str:
    """The LLM provider as recorded in reports: ``fake`` or the API host (api.groq.com)."""
    llm = settings.llm
    if llm.provider == "fake":
        return "fake"
    return urlparse(llm.base_url or "").hostname or llm.provider


def price_for(settings: Settings, model: str | None) -> ModelPrice:
    """The price of ``model``: model id, then provider host, then ``*``; default $0."""
    table = settings.pricing()
    for key in (model, provider_host(settings), "*"):
        if key and key in table:
            return table[key]
    return ModelPrice()


def cost_usd(price: ModelPrice, usage: TokenUsage) -> float:
    return round(
        (usage.input_tokens * price.input + usage.output_tokens * price.output) / 1_000_000, 8
    )


def priced(settings: Settings, model: str | None, usage: TokenUsage) -> TokenUsage:
    """``usage`` with ``cost_usd`` set at ``model``'s price."""
    return usage.model_copy(update={"cost_usd": cost_usd(price_for(settings, model), usage)})
