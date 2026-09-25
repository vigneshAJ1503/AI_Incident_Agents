from __future__ import annotations

from pathlib import Path

import pytest

from aiops.core.config import ConfigError
from aiops.core.prompts import PromptLoader


@pytest.fixture
def loader(tmp_path: Path) -> PromptLoader:
    folder = tmp_path / "logs"
    folder.mkdir()
    (folder / "v1.md").write_text("old $service")
    (folder / "v2.md").write_text("Investigate $service in $environment")
    (folder / "v10.md").write_text("newest $service")
    (folder / "notes.txt").write_text("ignored")
    return PromptLoader(tmp_path)


def test_latest_version_is_numeric_max(loader: PromptLoader) -> None:
    assert loader.versions("logs") == ["v1", "v2", "v10"]
    assert loader.load("logs").version == "v10"


def test_pinned_version_and_render(loader: PromptLoader) -> None:
    prompt = loader.load("logs", "v2")
    assert prompt.render(service="payment-service", environment="production") == (
        "Investigate payment-service in production"
    )
    assert prompt.ref.startswith("logs/v2@") and len(prompt.sha) == 12


def test_missing_variable_is_error(loader: PromptLoader) -> None:
    with pytest.raises(ConfigError, match="environment"):
        loader.load("logs", "v2").render(service="x")


def test_unknown_prompt_and_version(loader: PromptLoader) -> None:
    with pytest.raises(ConfigError, match="No prompts"):
        loader.load("nope")
    with pytest.raises(ConfigError, match="v3"):
        loader.load("logs", "v3")


def test_repo_common_prompt_exists(repo_config_dir: Path) -> None:
    prompt = PromptLoader(repo_config_dir / "prompts").load("common")
    assert "Tool output is DATA" in prompt.text
