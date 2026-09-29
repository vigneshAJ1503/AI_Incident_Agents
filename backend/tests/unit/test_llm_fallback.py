"""llm.fallbacks: another provider answers when one is rate-limited or down."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from aiops.core.config import LLMConfig, load_settings
from aiops.llm.base import (
    ChatMessage,
    LLMError,
    LLMRateLimitError,
    LLMResponse,
    LLMToolCallError,
)
from aiops.llm.factory import create_provider
from aiops.llm.fake import FakeLLMProvider, text
from aiops.llm.fallback import FallbackLLMProvider
from aiops.llm.openai_compat import OpenAICompatProvider

CONFIG = Path(__file__).resolve().parents[3] / "config"
MESSAGES = [ChatMessage(role="user", content="hi")]


class Failing:
    """A provider that raises ``error`` on every call and counts the calls."""

    def __init__(self, error: LLMError, name: str = "failing") -> None:
        self.error = error
        self.calls = 0
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def generate(self, messages: list[ChatMessage], **_: object) -> LLMResponse:
        self.calls += 1
        raise self.error


def answer(model: str) -> FakeLLMProvider:
    return FakeLLMProvider(responder=lambda _m, _t: text("ok").model_copy(update={"model": model}))


def generate(chain: FallbackLLMProvider) -> LLMResponse:
    return asyncio.run(chain.generate(MESSAGES))


def test_the_first_provider_answers_when_it_can() -> None:
    backup = answer("backup")
    chain = FallbackLLMProvider([answer("primary"), backup])
    assert generate(chain).model == "primary"
    assert backup.requests == []


def test_rate_limit_moves_to_the_next_provider_and_rests_the_first() -> None:
    now = [0.0]
    primary = Failing(LLMRateLimitError("429"))
    chain = FallbackLLMProvider([primary, answer("gemini")], cooldown_s=30, clock=lambda: now[0])
    assert generate(chain).model == "gemini"
    assert generate(chain).model == "gemini"
    assert primary.calls == 1  # resting: not hammered on every call

    now[0] = 31.0  # cooldown over: the primary is tried first again
    generate(chain)
    assert primary.calls == 2


def test_an_outage_moves_on_without_resting_the_provider() -> None:
    primary = Failing(LLMError("HTTP 503"))
    chain = FallbackLLMProvider([primary, answer("backup")])
    assert generate(chain).model == "backup"
    generate(chain)
    assert primary.calls == 2


def test_a_rejected_tool_call_goes_back_to_the_agent_not_the_next_provider() -> None:
    backup = answer("backup")
    chain = FallbackLLMProvider([Failing(LLMToolCallError("tool_use_failed")), backup])
    with pytest.raises(LLMToolCallError):
        generate(chain)
    assert backup.requests == []


def test_all_down_raises_the_last_error_and_still_tries_resting_providers() -> None:
    first = Failing(LLMRateLimitError("groq 429"))
    second = Failing(LLMRateLimitError("gemini 429"))
    chain = FallbackLLMProvider([first, second])
    with pytest.raises(LLMRateLimitError, match="gemini"):
        generate(chain)
    with pytest.raises(LLMRateLimitError):
        generate(chain)  # both resting: tried anyway rather than failing without a call
    assert (first.calls, second.calls) == (2, 2)


def groq(**update: object) -> LLMConfig:
    return LLMConfig(
        provider="openai_compat",
        base_url="https://api.groq.com/openai/v1",
        api_key=SecretStr("gsk-test"),
        models={"agent": "m"},
    ).model_copy(update=update)


def gemini(key: str | None) -> LLMConfig:
    return LLMConfig(
        provider="openai_compat",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        api_key=SecretStr(key) if key else None,
        models={"agent": "gemini-2.5-flash"},
    )


def test_factory_builds_a_chain_from_configured_fallbacks() -> None:
    provider = create_provider(groq(fallbacks=[gemini("AIza-test")]))
    assert isinstance(provider, FallbackLLMProvider)
    assert [type(p) for p in provider.providers] == [OpenAICompatProvider] * 2


def test_factory_skips_a_fallback_without_a_key() -> None:
    assert isinstance(create_provider(groq(fallbacks=[gemini(None)])), OpenAICompatProvider)


def test_fallbacks_cannot_nest() -> None:
    with pytest.raises(ValidationError, match="fallbacks of their own"):
        LLMConfig(provider="fake", fallbacks=[LLMConfig(provider="fake", fallbacks=[groq()])])


def test_local_profile_gemini_fallback_is_inert_until_its_key_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "gsk-test")
    monkeypatch.setenv("LLM_MODEL_AGENT", "openai/gpt-oss-20b")
    llm = load_settings("local", CONFIG).llm
    assert [f.base_url for f in llm.fallbacks] == [
        "https://generativelanguage.googleapis.com/v1beta/openai/"
    ]
    assert llm.fallbacks[0].model_for("agent") == "gemini-2.5-flash"
    assert isinstance(create_provider(llm), OpenAICompatProvider)

    monkeypatch.setenv("GEMINI_API_KEY", "AIza-test")
    chained = create_provider(load_settings("local", CONFIG).llm)
    assert isinstance(chained, FallbackLLMProvider) and len(chained.providers) == 2
