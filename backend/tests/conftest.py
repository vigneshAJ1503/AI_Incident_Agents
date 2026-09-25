from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_config_dir() -> Path:
    return REPO_ROOT / "config"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never depend on the developer's .env / shell."""
    for var in [
        "AIOPS_ENV",
        "AIOPS_CONFIG_DIR",
        "LLM_PROVIDER",
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_API_KEY",
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
    ]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("aiops.core.config.load_dotenv", lambda *a, **k: False)


def write_config(tmp_path: Path, env_yaml: str, catalog_yaml: str = "services: []\n") -> Path:
    config = tmp_path / "config"
    (config / "environments").mkdir(parents=True)
    (config / "service-catalog").mkdir()
    (config / "environments" / "test.yaml").write_text(env_yaml)
    (config / "service-catalog" / "local.yaml").write_text(catalog_yaml)
    return config
