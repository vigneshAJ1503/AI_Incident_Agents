from __future__ import annotations

from typer.testing import CliRunner

from aiops import __version__
from aiops.cli.main import app

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.stdout


def test_config_validate() -> None:
    result = runner.invoke(app, ["config", "validate"])
    assert result.exit_code == 0, result.output
    assert "logs" in result.stdout


def test_catalog_resolve_alias_with_environment() -> None:
    result = runner.invoke(app, ["catalog", "resolve", "payments api", "-E", "prod"])
    assert result.exit_code == 0, result.output
    assert "payment-prod-*" in result.stdout


def test_catalog_resolve_unknown_service_exits_1() -> None:
    result = runner.invoke(app, ["catalog", "resolve", "billing-engine"])
    assert result.exit_code == 1


def test_config_error_exits_2() -> None:
    result = runner.invoke(app, ["config", "validate", "--env", "does-not-exist"])
    assert result.exit_code == 2


def test_agent_run_rejects_invalid_hints() -> None:
    for bad in ("{not json", "[1, 2]"):
        args = ["agent", "run", "knowledge", "q", "-s", "payments", "--hints", bad]
        result = runner.invoke(app, args)
        assert result.exit_code == 2
        assert "hints" in result.output
