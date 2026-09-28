from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def pytest_configure(config: pytest.Config) -> None:
    """Disable .env loading for the WHOLE session, including module import/collection time.

    Test modules may build settings at import time, before any fixture runs; a developer's
    .env (which can hold a real LLM key) must never leak into tests.
    """
    import dotenv

    no_dotenv = lambda *a, **k: False  # noqa: E731
    dotenv.load_dotenv = no_dotenv  # type: ignore[assignment]
    import aiops.core.config as aiops_config

    aiops_config.load_dotenv = no_dotenv  # type: ignore[assignment]
    for var in (
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_API_KEY_2",
        "AIOPS_REPLAY_LLM",
        "LLM_MODEL_FAST",
        "LLM_MODEL_AGENT",
        "LLM_MODEL_RCA",
        "LLM_REASONING_EFFORT",
        "AIOPS_PROFILE",
        "AIOPS_ENV",
    ):
        os.environ.pop(var, None)


@pytest.fixture
def repo_config_dir() -> Path:
    return REPO_ROOT / "config"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never depend on the developer's .env / shell."""
    for var in [
        "AIOPS_ENV",
        "AIOPS_CONFIG_DIR",
        "AIOPS_PROFILE",
        "AIOPS_PROFILES_DIR",
        "LLM_PROVIDER",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_API_KEY",
        "OPENAI_COMPAT_API_KEY_2",
        "AIOPS_REPLAY_LLM",
        "LLM_MODEL_FAST",
        "LLM_MODEL_AGENT",
        "LLM_MODEL_RCA",
        "LOGS_MCP_URL",
        "KIBANA_URL",
        "KNOWLEDGE_MCP_URL",
        "KNOWLEDGE_DATABASE_URL",
        "TICKETS_PROVIDER",
        "TICKETS_MCP_URL",
        "TICKETS_PROJECT_KEY",
        "TICKETS_UI_URL",
        "AIOPS_API_KEY",
        "AIOPS_API_CORS_ORIGINS",
        "AIOPS_ENABLE_FAULTS",
        "LLM_REASONING_EFFORT",
        "AIOPS_API_KEYS",
    ]:
        monkeypatch.delenv(var, raising=False)
    # No test may read a developer's .env (it can hold a real LLM key). Block every
    # loader, not just the config one: CLI modules load it too.
    no_dotenv = lambda *a, **k: False  # noqa: E731
    monkeypatch.setattr("dotenv.load_dotenv", no_dotenv)
    monkeypatch.setattr("aiops.core.config.load_dotenv", no_dotenv)
    for module in ("aiops.cli.knowledge_cmd", "aiops.cli.seed_cmd"):
        with contextlib.suppress(ImportError, AttributeError):
            monkeypatch.setattr(f"{module}.load_dotenv", no_dotenv)


@pytest.fixture(autouse=True)
def _restore_environ() -> Iterator[None]:
    """Whatever a test puts into os.environ (directly or via a loader) is undone afterwards."""
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _fresh_circuit_breakers() -> None:
    """Circuit breakers are process-wide: each test starts with every circuit closed."""
    from aiops.mcp.breaker import BREAKERS

    BREAKERS.reset()


def write_config(tmp_path: Path, env_yaml: str, catalog_yaml: str = "services: []\n") -> Path:
    config = tmp_path / "config"
    (config / "environments").mkdir(parents=True)
    (config / "service-catalog").mkdir()
    (config / "environments" / "test.yaml").write_text(env_yaml)
    (config / "service-catalog" / "local.yaml").write_text(catalog_yaml)
    return config
