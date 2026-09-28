"""Enterprise LLM providers (PR-P4b): anthropic, bedrock, azure_openai.

Every test runs the real adapter + vendor SDK against a mocked transport (tests/unit/
llm_wire.py): zero tokens, no network, no keys.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, SecretStr, ValidationError

from aiops.core.config import ConfigError, LLMConfig
from aiops.core.models import TokenUsage
from aiops.llm.base import ChatMessage, LLMError, LLMProvider, LLMRateLimitError, ToolSpec
from aiops.llm.factory import config_problems, create_provider, llm_configured
from aiops.llm.fake import text, tool_call
from aiops.llm.structured import generate_structured
from tests.unit import llm_wire as w

# --------------------------------------------------------------------------- cases


@dataclass(frozen=True)
class Case:
    name: str
    #: (responder, override, config updates, attempts) -> (provider, recorder)
    build: Callable[..., tuple[LLMProvider, w.Recorder]]
    rate_limited: tuple[int, dict[str, Any], dict[str, str]]
    server_error: tuple[int, dict[str, Any], dict[str, str]]
    auth_error: tuple[int, dict[str, Any], dict[str, str]]
    secret: str


def _anthropic(responder: w.Responder, override: Any = None, retries: int = 0) -> Any:
    return w.anthropic_server(responder, override, w.anthropic_config(max_retries=retries))


def _azure(responder: w.Responder, override: Any = None, retries: int = 0) -> Any:
    return w.azure_server(responder, override, w.azure_config(max_retries=retries))


def _bedrock(responder: w.Responder, override: Any = None, retries: int = 0) -> Any:
    return w.bedrock_server(
        responder, override, w.bedrock_config(max_retries=retries), max_attempts=retries + 1
    )


FAST = {"retry-after-ms": "1"}  # SDK backoff honours it: keeps retry tests instant
CASES = [
    Case(
        "anthropic",
        _anthropic,
        (429, w.anthropic_error(429, "rate_limit_error", "slow down"), FAST),
        (529, w.anthropic_error(529, "overloaded_error", "Overloaded"), FAST),
        (401, w.anthropic_error(401, "authentication_error", f"bad key {w.ANTHROPIC_KEY}"), {}),
        w.ANTHROPIC_KEY,
    ),
    Case(
        "azure_openai",
        _azure,
        (429, {"error": {"message": "slow down", "code": "429"}}, FAST),
        (500, {"error": {"message": "internal error"}}, FAST),
        (401, {"error": {"message": f"Access denied for key {w.AZURE_KEY}"}}, {}),
        w.AZURE_KEY,
    ),
    Case(
        "bedrock",
        _bedrock,
        (429, {"message": "Too many requests"}, {"x-amzn-ErrorType": "ThrottlingException"}),
        (503, {"message": "busy"}, {"x-amzn-ErrorType": "ServiceUnavailableException"}),
        (
            403,
            {"message": "The security token included in the request is invalid."},
            {"x-amzn-ErrorType": "UnrecognizedClientException"},
        ),
        w.AWS_SECRET_KEY,
    ),
]
IDS = [c.name for c in CASES]


@pytest.fixture(autouse=True)
def _instant_botocore_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("botocore.endpoint.time", w.no_botocore_sleep())


def fixed(response: Any) -> w.Responder:
    return lambda messages, tools: response


def always(answer: tuple[int, dict[str, Any], dict[str, str]]) -> w.Override:
    return lambda n: answer


# --------------------------------------------------------------------------- behaviour


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_plain_completion_uses_role_model_and_counts_usage(case: Case) -> None:
    provider, rec = case.build(fixed(text("pong")))
    response = await provider.generate(
        [ChatMessage.system("Be brief."), ChatMessage.user("ping")], role="fast", max_tokens=5
    )
    assert provider.name == case.name
    assert response.content == "pong" and response.tool_calls == []
    assert response.usage == TokenUsage(input_tokens=7, output_tokens=3, calls=1)
    assert response.finish_reason == "stop"
    (seen,) = rec.requests
    body = seen["body"]
    if case.name == "anthropic":
        assert body["model"] == "fast-model" and body["max_tokens"] == 5
        assert body["system"] == "Be brief."
        assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "ping"}]}]
        assert "temperature" not in body and "tools" not in body
        assert seen["headers"]["x-api-key"] == w.ANTHROPIC_KEY
        assert seen["url"] == "https://api.anthropic.com/v1/messages"
    elif case.name == "azure_openai":
        assert seen["url"] == (
            "https://acme-ai.openai.azure.com/openai/deployments/fast-model/chat/completions"
            "?api-version=2024-10-21"
        )
        assert seen["headers"]["api-key"] == w.AZURE_KEY
        assert body["messages"][0] == {"role": "system", "content": "Be brief."}
    else:
        assert seen["url"] == (
            "https://bedrock-runtime.us-east-1.amazonaws.com/model/fast-model/converse"
        )
        assert body["system"] == [{"text": "Be brief."}]
        assert body["messages"] == [{"role": "user", "content": [{"text": "ping"}]}]
        assert body["inferenceConfig"] == {"maxTokens": 5}
        assert "toolConfig" not in body
        assert w.AWS_SECRET_KEY not in str(seen["headers"])  # SigV4 signs, never sends it


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_tool_calls_round_trip(case: Case) -> None:
    scripted = tool_call("search_logs", {"level": "ERROR", "limit": 5}, call_id="call-1")
    provider, rec = case.build(fixed(scripted))
    tools = [
        ToolSpec(
            name="search_logs",
            description="Search logs",
            parameters={"type": "object", "properties": {"level": {"type": "string"}}},
        )
    ]
    first = await provider.generate([ChatMessage.user("q")], tools=tools, tool_choice="required")
    assert first.tool_calls[0].name == "search_logs"
    assert first.tool_calls[0].arguments == {"level": "ERROR", "limit": 5}
    assert first.tool_calls[0].id == "call-1"
    assert first.finish_reason == "tool_calls"

    # The assistant tool call + two tool results + a user nudge serialize back.
    history = [
        ChatMessage.user("q"),
        first.as_message(),
        ChatMessage.tool_result("call-1", "3 hits"),
        ChatMessage.user("Now submit."),
    ]
    await provider.generate(history, tools=tools)
    body = rec.bodies[1]
    if case.name == "anthropic":
        assert rec.bodies[0]["tool_choice"] == {"type": "any"}
        assert rec.bodies[0]["tools"][0]["input_schema"]["properties"]["level"]
        assert body["tool_choice"] == {"type": "auto"}
        assert [t["role"] for t in body["messages"]] == ["user", "assistant", "user"]
        assert body["messages"][1]["content"] == [
            {
                "type": "tool_use",
                "id": "call-1",
                "name": "search_logs",
                "input": {"level": "ERROR", "limit": 5},
            }
        ]
        # tool results and the follow-up text share one user turn
        assert body["messages"][2]["content"] == [
            {"type": "tool_result", "tool_use_id": "call-1", "content": "3 hits"},
            {"type": "text", "text": "Now submit."},
        ]
    elif case.name == "azure_openai":
        assert rec.bodies[0]["tool_choice"] == "required"
        assert body["messages"][2] == {
            "role": "tool",
            "content": "3 hits",
            "tool_call_id": "call-1",
        }
        assert body["messages"][1]["tool_calls"][0]["function"]["name"] == "search_logs"
    else:
        assert rec.bodies[0]["toolConfig"]["toolChoice"] == {"any": {}}
        assert body["toolConfig"]["toolChoice"] == {"auto": {}}
        assert [t["role"] for t in body["messages"]] == ["user", "assistant", "user"]
        assert body["messages"][1]["content"] == [
            {
                "toolUse": {
                    "toolUseId": "call-1",
                    "name": "search_logs",
                    "input": {"level": "ERROR", "limit": 5},
                }
            }
        ]
        assert body["messages"][2]["content"] == [
            {"toolResult": {"toolUseId": "call-1", "content": [{"text": "3 hits"}]}},
            {"text": "Now submit."},
        ]


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_tool_choice_none_and_unforced(case: Case) -> None:
    provider, rec = case.build(fixed(text("ok")))
    tools = [ToolSpec(name="t", description="d")]
    await provider.generate([ChatMessage.user("q")], tools=tools, tool_choice="none")
    body = rec.bodies[0]
    if case.name == "anthropic":
        assert body["tool_choice"] == {"type": "none"}
    elif case.name == "azure_openai":
        assert body["tool_choice"] == "none"
    else:  # Converse has no "none": tools are left out when the history doesn't need them
        assert "toolConfig" not in body

    # forced_tool_choice: false sends "required" as auto (models that reject forced use)
    unforced = {
        "anthropic": lambda: w.anthropic_server(
            fixed(text("ok")), None, w.anthropic_config(forced_tool_choice=False)
        ),
        "azure_openai": lambda: w.azure_server(
            fixed(text("ok")), None, w.azure_config(forced_tool_choice=False)
        ),
        "bedrock": lambda: w.bedrock_server(
            fixed(text("ok")), None, w.bedrock_config(forced_tool_choice=False)
        ),
    }[case.name]
    provider, rec = unforced()
    await provider.generate([ChatMessage.user("q")], tools=tools, tool_choice="required")
    body = rec.bodies[0]
    choice = body.get("tool_choice") or body["toolConfig"]["toolChoice"]
    assert choice in ({"type": "auto"}, "auto", {"auto": {}})


class Verdict(BaseModel):
    service: str
    confidence: float


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_generate_structured_submit_pattern(case: Case) -> None:
    """The ``submit`` tool pattern every agent uses, with its corrective retry."""
    script = iter(
        [
            tool_call("submit", {"service": "payment-service"}, call_id="s1"),  # invalid
            tool_call("submit", {"service": "payment-service", "confidence": 0.9}, call_id="s2"),
        ]
    )
    provider, rec = case.build(lambda messages, tools: next(script))
    result, usage = await generate_structured(provider, [ChatMessage.user("q")], Verdict)
    assert result == Verdict(service="payment-service", confidence=0.9)
    assert usage == TokenUsage(input_tokens=14, output_tokens=6, calls=2)
    assert len(rec.requests) == 2
    # the retry carries the rejected call + its tool result back in vendor format
    assert "rejected" in str(rec.bodies[1]["messages"][-1]).lower()


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_429_becomes_rate_limit_error_after_retries(case: Case) -> None:
    provider, rec = case.build(fixed(text("x")), always(case.rate_limited), retries=1)
    with pytest.raises(LLMRateLimitError, match=case.name):
        await provider.generate([ChatMessage.user("q")])
    assert len(rec.requests) == 2  # the SDK/botocore retried once, then gave up


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_5xx_is_retried_then_succeeds(case: Case) -> None:
    first_fails = lambda n: case.server_error if n == 1 else None  # noqa: E731
    provider, rec = case.build(fixed(text("recovered")), first_fails, retries=2)
    response = await provider.generate([ChatMessage.user("q")])
    assert response.content == "recovered"
    assert len(rec.requests) == 2


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_5xx_without_retries_is_llm_error(case: Case) -> None:
    provider, _ = case.build(fixed(text("x")), always(case.server_error), retries=0)
    with pytest.raises(LLMError) as info:
        await provider.generate([ChatMessage.user("q")])
    assert not isinstance(info.value, LLMRateLimitError)
    assert str(case.server_error[0]) in str(info.value)


@pytest.mark.parametrize("case", CASES, ids=IDS)
async def test_secrets_never_in_errors_or_logs(
    case: Case, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)  # SDK, httpx and botocore debug logs included
    provider, _ = case.build(fixed(text("x")), always(case.auth_error))
    with pytest.raises(LLMError) as info:
        await provider.generate([ChatMessage.user("q")])
    assert case.secret not in str(info.value)
    assert info.value.__cause__ is None and info.value.__suppress_context__
    assert case.secret not in caplog.text
    if case.name != "bedrock":  # the fake server echoed the key: it was masked
        assert "********" in str(info.value)


async def test_anthropic_explicit_temperature_cache_usage_and_gateway() -> None:
    def respond(messages: Any, tools: Any) -> Any:
        return text("hi")

    config = w.anthropic_config(temperature=0.2, base_url="https://llm-gateway.acme.test")
    provider, rec = w.anthropic_server(respond, None, config)
    await provider.generate([ChatMessage.user("q")])
    assert rec.bodies[0]["temperature"] == 0.2
    assert rec.requests[0]["url"] == "https://llm-gateway.acme.test/v1/messages"

    from aiops.llm.anthropic_messages import from_wire

    class Usage:
        input_tokens, output_tokens = 10, 4
        cache_creation_input_tokens, cache_read_input_tokens = 100, 1000

    class Message:
        content: tuple[Any, ...] = ()
        usage = Usage()
        model = "m"
        stop_reason = "max_tokens"

    parsed = from_wire(Message(), "m")
    assert parsed.usage == TokenUsage(input_tokens=1110, output_tokens=4, calls=1)
    assert parsed.finish_reason == "length"


async def test_bedrock_access_denied_hint_and_none_with_tool_history() -> None:
    denied = (403, {"message": "no access"}, {"x-amzn-ErrorType": "AccessDeniedException"})
    provider, _ = w.bedrock_server(fixed(text("x")), always(denied))
    with pytest.raises(LLMError, match="model access"):
        await provider.generate([ChatMessage.user("q")])

    # tool_choice none with tool blocks in history: toolConfig must stay (Converse rule)
    provider, rec = w.bedrock_server(fixed(text("done")))
    history = [
        ChatMessage.user("q"),
        tool_call("t", {}, call_id="c1").as_message(),
        ChatMessage.tool_result("c1", "r"),
    ]
    await provider.generate(
        history, tools=[ToolSpec(name="t", description="d")], tool_choice="none"
    )
    assert "toolChoice" not in rec.bodies[0]["toolConfig"]


# --------------------------------------------------------------------------- config


def test_factory_builds_each_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    assert create_provider(w.anthropic_config()).name == "anthropic"
    assert create_provider(w.azure_config()).name == "azure_openai"
    assert create_provider(w.bedrock_config()).name == "bedrock"


def test_readable_config_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        create_provider(w.anthropic_config(api_key=None))
    with pytest.raises(ConfigError, match=r"api_version is not set \(AZURE_OPENAI_API_VERSION\)"):
        create_provider(w.azure_config(api_version=None))
    with pytest.raises(ConfigError, match="AZURE_OPENAI_ENDPOINT"):
        create_provider(w.azure_config(base_url=None))
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/nonexistent/aws-config")
    with pytest.raises(ConfigError, match="AWS_REGION"):
        create_provider(w.bedrock_config())
    assert "no AWS region" in " ".join(config_problems(w.bedrock_config()))
    assert not llm_configured(w.bedrock_config())


def test_missing_lists_fields_and_env_vars() -> None:
    assert w.anthropic_config().missing() == []
    assert w.anthropic_config(api_key=None, models={}).missing() == [
        "llm.api_key is not set (ANTHROPIC_API_KEY)",
        "llm.models.agent (model name) is not set (LLM_MODEL_AGENT)",
    ]
    assert w.azure_config(models={}).missing() == [
        "llm.models.agent (deployment name) is not set (LLM_MODEL_AGENT)"
    ]
    assert w.bedrock_config().missing() == []  # no key: the AWS chain
    assert LLMConfig(provider="fake").missing() == []
    assert w.anthropic_config(models={"agent": "a", "rca": "r"}).models_by_role() == {
        "fast": "a",
        "agent": "a",
        "rca": "r",
    }


def test_bedrock_rejects_keys_in_yaml_and_api_version_is_azure_only() -> None:
    with pytest.raises(ValidationError, match="standard AWS chain"):
        LLMConfig(provider="bedrock", api_key=SecretStr("nope"))
    with pytest.raises(ValidationError, match="azure_openai"):
        LLMConfig(provider="anthropic", api_version="2024-10-21")


def test_llm_configured_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert llm_configured(w.anthropic_config())
    assert llm_configured(w.azure_config())
    assert llm_configured(w.bedrock_config())
    assert not llm_configured(w.anthropic_config(api_key=SecretStr(" ")))
    assert not llm_configured(LLMConfig(provider="fake"))


# --------------------------------------------------------------------------- aiops llm ping

PING_PROFILE = """
llm:
  provider: anthropic
  api_key: ${TEST_PING_KEY:-}
  models: {fast: fast-model, agent: agent-model}
"""


def test_llm_ping_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from aiops.cli import llm_cmd
    from aiops.cli.main import app
    from tests.conftest import write_config

    monkeypatch.setenv("AIOPS_CONFIG_DIR", str(write_config(tmp_path, PING_PROFILE)))
    monkeypatch.delenv("TEST_PING_KEY", raising=False)
    result = CliRunner().invoke(app, ["llm", "ping", "--profile", "test"])
    assert result.exit_code == 1
    assert "llm.api_key is not set (ANTHROPIC_API_KEY)" in " ".join(result.output.split())

    monkeypatch.setenv("TEST_PING_KEY", w.ANTHROPIC_KEY)
    rec: list[w.Recorder] = []

    def wired(config: LLMConfig) -> LLMProvider:
        provider, recorder = w.anthropic_server(fixed(text("pong")), None, config)
        rec.append(recorder)
        return provider

    monkeypatch.setattr(llm_cmd, "create_provider", wired)
    result = CliRunner().invoke(app, ["llm", "ping", "--profile", "test"])
    assert result.exit_code == 0, result.output
    assert "provider=anthropic role=fast model=fast-model reply='pong'" in result.output
    assert "tokens=10" in result.output
    assert w.ANTHROPIC_KEY not in result.output
    assert rec[0].bodies[0]["model"] == "fast-model"
