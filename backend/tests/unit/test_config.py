from __future__ import annotations

import json
from pathlib import Path

import pytest

from aiops.core.config import ConfigError, interpolate, load_settings
from tests.conftest import write_config

MINIMAL = """
llm:
  provider: fake
capabilities:
  logs:
    provider: elasticsearch
    mcp: {transport: http, url: "${LOGS_URL:-http://localhost:8101/mcp}"}
    tool_allowlist: [search_logs]
"""


def test_repo_local_config_is_valid(repo_config_dir: Path) -> None:
    settings = load_settings("local", repo_config_dir)
    assert settings.environment == "local"
    logs = settings.capability("logs")
    assert logs.provider == "elasticsearch"
    assert "search_logs" in logs.tool_allowlist
    assert settings.guardrails.read_only is True


def test_interpolation_required_default_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SET_VAR", "value")
    missing: list[str] = []
    out = interpolate(
        {"a": "${SET_VAR}", "b": ["${UNSET:-fallback}", "x-${UNSET_REQUIRED}"], "c": 3}, missing
    )
    assert out == {"a": "value", "b": ["fallback", "x-"], "c": 3}
    assert missing == ["UNSET_REQUIRED"]


def test_missing_required_env_var_fails_fast(tmp_path: Path) -> None:
    config = write_config(
        tmp_path, MINIMAL.replace("${LOGS_URL:-http://localhost:8101/mcp}", "${LOGS_URL}")
    )
    with pytest.raises(ConfigError, match="LOGS_URL"):
        load_settings("test", config)


def test_invalid_config_has_readable_error(tmp_path: Path) -> None:
    config = write_config(
        tmp_path, MINIMAL.replace("transport: http, url: ", "transport: http, urlx: ")
    )
    with pytest.raises(ConfigError) as exc:
        load_settings("test", config)
    message = str(exc.value)
    assert "capabilities.logs.mcp" in message
    assert "test.yaml" in message


def test_empty_allowlist_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path, MINIMAL.replace("[search_logs]", "[]"))
    with pytest.raises(ConfigError, match="tool_allowlist"):
        load_settings("test", config)


def test_unknown_capability_error_lists_configured(tmp_path: Path) -> None:
    settings = load_settings("test", write_config(tmp_path, MINIMAL))
    with pytest.raises(ConfigError, match="configured: logs"):
        settings.capability("metrics")


def test_secrets_are_masked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KEY", "super-secret-value")
    config = write_config(
        tmp_path, MINIMAL.replace("provider: fake", "provider: fake\n  api_key: ${KEY}")
    )
    settings = load_settings("test", config)
    assert settings.llm.api_key is not None
    assert settings.llm.api_key.get_secret_value() == "super-secret-value"
    dumped = json.dumps(settings.safe_dump())
    assert "super-secret-value" not in dumped
    assert "super-secret-value" not in repr(settings)


def test_empty_optional_values_become_unset(tmp_path: Path) -> None:
    yaml_text = MINIMAL.replace("provider: fake", "provider: fake\n  api_key: ${NOPE:-}")
    settings = load_settings("test", write_config(tmp_path, yaml_text))
    assert settings.llm.api_key is None


def test_model_for_role_falls_back_to_agent(tmp_path: Path) -> None:
    yaml_text = MINIMAL.replace("provider: fake", "provider: fake\n  models: {agent: big-model}")
    settings = load_settings("test", write_config(tmp_path, yaml_text))
    assert settings.llm.model_for("rca") == "big-model"


def test_model_for_role_without_models_is_config_error(tmp_path: Path) -> None:
    settings = load_settings("test", write_config(tmp_path, MINIMAL))
    with pytest.raises(ConfigError, match="LLM_MODEL_AGENT"):
        settings.llm.model_for("agent")


def test_missing_environment_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings("nope", write_config(tmp_path, MINIMAL))
