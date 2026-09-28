"""Company profiles (PR-P1, ADR-0011): selection, extends, back-compat, validate, init, diff."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiops.agents.deps import build_deps
from aiops.cli.main import app
from aiops.core import config as config_module
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import ConfigError, load_settings, load_settings_lenient
from aiops.core.profiles import (
    MASK,
    ValidationReport,
    catalog_summary,
    check_env_example,
    check_settings,
    diff_dumps,
    init_profile,
    mask_secrets,
    resolved_dump,
)
from aiops.llm.fake import FakeLLMProvider
from tests.conftest import REPO_ROOT, write_config

REPO_CONFIG = REPO_ROOT / "config"
REPO_PROFILES = REPO_ROOT / "profiles"

#: Everything the template requires, as a company would set it in profiles/<name>/.env.
TEMPLATE_ENV = {
    "OPENAI_COMPAT_BASE_URL": "https://llm.example.com/v1",
    "OPENAI_COMPAT_API_KEY": "sk-test-not-a-real-key",
    "LLM_MODEL_AGENT": "some-model",
    "LOGS_MCP_URL": "http://logs.example.com/mcp",
    "METRICS_MCP_URL": "http://metrics.example.com/mcp",
    "ALERTS_MCP_URL": "http://alerts.example.com/mcp",
    "K8S_MCP_URL": "http://k8s.example.com/mcp",
    "CODE_MCP_URL": "http://code.example.com/mcp",
    "TICKETS_MCP_URL": "http://tickets.example.com/mcp",
    "KNOWLEDGE_MCP_URL": "http://kb.example.com/mcp",
    "TICKETS_PROJECT_KEY": "INC",
    "JIRA_URL": "https://acme.atlassian.net",
    "KIBANA_URL": "https://kibana.example.com",
    "GRAFANA_URL": "https://grafana.example.com",
    "PROMETHEUS_URL": "https://prometheus.example.com",
    "ALERTMANAGER_URL": "https://alertmanager.example.com",
}

MINIMAL_PROFILE = """
metadata: {company: Acme, description: acme prod, owners: [sre@acme.test]}
llm: {provider: fake}
capabilities:
  logs:
    provider: elasticsearch
    mcp: {transport: http, url: "http://localhost:8101/mcp"}
    tool_allowlist: [execute_esql]
    settings: {index_pattern: "logs-*"}
"""

CATALOG = """
environments: {production: {aliases: [prod]}}
services:
  - name: checkout
    aliases: [cart]
    capabilities: {logs: {service_value: checkout}}
"""


@pytest.fixture
def profiles(tmp_path: Path) -> Path:
    """A repo-like tmp dir: config/prompts (shared) + profiles/acme."""
    (tmp_path / "config" / "prompts" / "logs").mkdir(parents=True)
    (tmp_path / "config" / "prompts" / "logs" / "v1.md").write_text("shared v1")
    (tmp_path / "config" / "prompts" / "common").mkdir()
    (tmp_path / "config" / "prompts" / "common" / "v1.md").write_text("common")
    acme = tmp_path / "profiles" / "acme"
    acme.mkdir(parents=True)
    (acme / "profile.yaml").write_text(MINIMAL_PROFILE)
    (acme / "services.yaml").write_text(CATALOG)
    return tmp_path / "profiles"


def config_of(profiles: Path) -> Path:
    return profiles.parent / "config"


# --------------------------------------------------------------------------- repo profiles


def test_repo_profiles_load_from_profiles_folder() -> None:
    local = load_settings("local", REPO_CONFIG)
    assert local.profile == "local" and local.profile_dir == REPO_PROFILES / "local"
    assert local.catalog_path() == REPO_PROFILES / "local" / "services.yaml"
    assert local.metadata.company
    k8s = load_settings("local-k8s", REPO_CONFIG)
    assert [p.name for p in k8s.profile_chain] == ["local-k8s", "local"]
    assert k8s.metadata.description != local.metadata.description  # never inherited


@pytest.mark.parametrize("name", ["local", "local-k8s"])
def test_repo_profiles_validate_without_errors(name: str) -> None:
    settings = load_settings(name, REPO_CONFIG)
    report = check_settings(settings, ServiceCatalog.from_settings(settings))
    check_env_example(settings.profile_chain, report)
    assert report.errors == []
    # Only the "no LLM key on this machine" warnings are acceptable.
    assert all(i.message.startswith("llm:") for i in report.warnings), report.warnings


def test_template_is_complete_and_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in TEMPLATE_ENV.items():
        monkeypatch.setenv(key, value)
    settings = load_settings("_template", REPO_CONFIG)
    report = check_settings(settings, ServiceCatalog.from_settings(settings))
    check_env_example(settings.profile_chain, report)
    assert report.issues == []
    assert set(settings.capabilities) == {
        "logs",
        "metrics",
        "alerts",
        "k8s",
        "code",
        "tickets",
        "knowledge",
    }


def test_template_lists_its_required_variables() -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings("_template", REPO_CONFIG)
    message = str(exc.value)
    assert "profile '_template'" in message and "OPENAI_COMPAT_API_KEY" in message
    assert ".env.example" in message


# --------------------------------------------------------------------------- selection


def test_aiops_profile_selects(profiles: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIOPS_PROFILE", "acme")
    assert load_settings(None, config_of(profiles)).profile == "acme"


def test_aiops_env_is_a_deprecated_alias(
    profiles: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(config_module, "_warned_env_alias", False)
    monkeypatch.setenv("AIOPS_ENV", "acme")
    with caplog.at_level(logging.WARNING):
        assert load_settings(None, config_of(profiles)).profile == "acme"
    assert "AIOPS_ENV is deprecated; use AIOPS_PROFILE=acme" in caplog.text


def test_precedence_explicit_then_profile_then_env(
    profiles: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(profiles / "acme", profiles / "other")
    monkeypatch.setenv("AIOPS_ENV", "nope")
    monkeypatch.setenv("AIOPS_PROFILE", "acme")
    assert load_settings(None, config_of(profiles)).profile == "acme"
    assert load_settings("other", config_of(profiles)).profile == "other"


def test_profiles_dir_can_live_outside_the_repo(
    profiles: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private = tmp_path / "private-profiles"
    shutil.move(profiles / "acme", private / "acme")
    monkeypatch.setenv("AIOPS_PROFILES_DIR", str(private))
    settings = load_settings("acme", config_of(profiles))
    assert settings.profile_dir == private / "acme"
    assert ServiceCatalog.from_settings(settings).get("checkout")


def test_unknown_profile_lists_available_and_suggests_init(profiles: Path) -> None:
    with pytest.raises(ConfigError) as exc:
        load_settings("acm", config_of(profiles))
    message = str(exc.value)
    assert "Profile 'acm' not found" in message
    assert "Available: acme" in message
    assert "aiops profile init acm" in message


# --------------------------------------------------------------------------- back-compat


def test_legacy_environment_file_still_loads(tmp_path: Path) -> None:
    config = write_config(tmp_path, MINIMAL_PROFILE, CATALOG)
    settings = load_settings("test", config)
    assert settings.profile_chain == ()
    assert settings.catalog_path() == config / "service-catalog" / "local.yaml"
    assert ServiceCatalog.from_settings(settings).get("checkout")


def test_profile_wins_over_legacy_file_of_the_same_name(profiles: Path) -> None:
    config = config_of(profiles)
    (config / "environments").mkdir()
    (config / "environments" / "acme.yaml").write_text("llm: {provider: nope}\n")
    assert load_settings("acme", config).profile_dir == profiles / "acme"


def test_legacy_name_listed_when_not_found(tmp_path: Path) -> None:
    config = write_config(tmp_path, MINIMAL_PROFILE)
    with pytest.raises(ConfigError, match=r"test \(legacy\)"):
        load_settings("nope", config)


# --------------------------------------------------------------------------- extends


def test_child_profile_inherits_config_and_catalog(profiles: Path) -> None:
    child = profiles / "acme-staging"
    child.mkdir()
    (child / "profile.yaml").write_text(
        "extends: acme\nmetadata: {company: Acme}\n"
        "capabilities: {logs: {settings: {index_pattern: 'staging-*'}}}\n"
    )
    settings = load_settings("acme-staging", config_of(profiles))
    assert settings.capability("logs").tool_allowlist == ["execute_esql"]
    assert settings.capability("logs").settings["index_pattern"] == "staging-*"
    assert settings.metadata.description == ""  # metadata is never inherited
    assert settings.service_catalog == "acme"  # no own services.yaml -> the parent's
    assert ServiceCatalog.from_settings(settings).get("checkout")


def test_child_catalog_extends_parent_catalog(profiles: Path) -> None:
    child = profiles / "acme-k8s"
    child.mkdir()
    (child / "profile.yaml").write_text("extends: acme\n")
    (child / "services.yaml").write_text(
        "extends: acme\nservices:\n  - name: checkout\n    aliases: [basket]\n  - name: new-svc\n"
    )
    settings = load_settings("acme-k8s", config_of(profiles))
    catalog = ServiceCatalog.from_settings(settings)
    assert settings.service_catalog == "acme-k8s"
    assert catalog.get("checkout").aliases == ["basket"]
    assert catalog.get("checkout").identifiers("logs") == {"service_value": "checkout"}
    assert [s.name for s in catalog.services] == ["checkout", "new-svc"]


def test_circular_profile_extends_is_readable(profiles: Path) -> None:
    (profiles / "a").mkdir()
    (profiles / "a" / "profile.yaml").write_text("extends: b\n")
    (profiles / "b").mkdir()
    (profiles / "b" / "profile.yaml").write_text("extends: a\n")
    with pytest.raises(ConfigError, match=r"Circular 'extends'.*a -> b -> a"):
        load_settings("a", config_of(profiles))


def test_profile_env_file_is_loaded(profiles: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[Path] = []
    monkeypatch.setattr(
        "aiops.core.config.load_dotenv", lambda path, **_: loaded.append(path) or False
    )
    load_settings("acme", config_of(profiles))
    assert loaded == [profiles / "acme" / ".env", profiles.parent / ".env"]


# --------------------------------------------------------------------------- prompts


def test_profile_prompts_override_and_extend_shared(profiles: Path) -> None:
    folder = profiles / "acme" / "prompts" / "logs"
    folder.mkdir(parents=True)
    (folder / "v1.md").write_text("acme v1")
    (folder / "v2.md").write_text("acme v2")
    settings = load_settings("acme", config_of(profiles))
    deps = build_deps(settings, llm=FakeLLMProvider([]))
    assert deps.prompts.versions("logs") == ["v1", "v2"]
    assert deps.prompts.load("logs").text == "acme v2"
    assert deps.prompts.load("logs", "v1").text == "acme v1"
    assert deps.prompts.load("common").text == "common"  # falls back to shared
    assert deps.prompts.names() == ["common", "logs"]


# --------------------------------------------------------------------------- validate


def _check(profiles: Path, capability_yaml: str) -> ValidationReport:
    (profiles / "acme" / "profile.yaml").write_text(
        "llm: {provider: fake}\ncapabilities:\n" + capability_yaml
    )
    settings = load_settings("acme", config_of(profiles))
    return check_settings(settings, ServiceCatalog.from_settings(settings))


def test_planned_provider_names_missing_setting(profiles: Path) -> None:
    report = _check(
        profiles,
        "  logs: {provider: splunk, mcp: {transport: http, url: 'http://x'},"
        " tool_allowlist: [query]}\n",
    )
    messages = [i.message for i in report.errors]
    assert (
        "capability logs: provider 'splunk' needs setting 'index' "
        "(capabilities.logs.settings.index)" in messages
    )
    assert any("provider 'splunk' is planned, not implemented yet" in m for m in messages)


def test_implemented_loki_names_missing_setting(profiles: Path) -> None:
    report = _check(
        profiles,
        "  logs: {provider: loki, mcp: {transport: http, url: 'http://x'},"
        " tool_allowlist: [query, query_range]}\n",
    )
    messages = [i.message for i in report.errors]
    assert (
        "capability logs: provider 'loki' needs setting 'stream_labels' "
        "(capabilities.logs.settings.stream_labels)" in messages
    )
    assert not any("planned" in m for m in messages)


def test_unknown_provider_lists_implemented(profiles: Path) -> None:
    report = _check(
        profiles,
        "  metrics: {provider: graphite, mcp: {transport: http, url: 'http://x'},"
        " tool_allowlist: [query]}\n",
    )
    assert any(
        "unknown provider 'graphite' (implemented: prometheus" in i.message for i in report.errors
    )


def test_jira_needs_project_key_and_allowlist_warning(profiles: Path) -> None:
    report = _check(
        profiles,
        "  tickets: {provider: jira, mcp: {transport: http, url: 'http://x'},"
        " tool_allowlist: [jira_get_issue]}\n",
    )
    assert [i.message for i in report.errors] == [
        "capability tickets: provider 'jira' needs setting 'project_key' "
        "(capabilities.tickets.settings.project_key)"
    ]
    assert any("tool_allowlist lacks ['jira_search']" in i.message for i in report.warnings)


def test_catalog_without_index_pattern_warns(profiles: Path) -> None:
    report = _check(
        profiles,
        "  logs: {provider: elasticsearch, mcp: {transport: http, url: 'http://x'},"
        " tool_allowlist: [execute_esql]}\n",
    )
    assert report.errors == []
    assert any("service checkout: no logs.index_pattern" in i.message for i in report.warnings)


def test_undocumented_env_var_warns(profiles: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_LOGS_URL", "http://x")
    (profiles / "acme" / "profile.yaml").write_text(
        MINIMAL_PROFILE.replace('"http://localhost:8101/mcp"', '"${ACME_LOGS_URL}"')
    )
    settings = load_settings("acme", config_of(profiles))
    report = ValidationReport("acme")
    check_env_example(settings.profile_chain, report)
    assert "does not list ACME_LOGS_URL" in report.warnings[0].message
    (profiles / "acme" / ".env.example").write_text("# the logs MCP\nACME_LOGS_URL=\n")
    report = ValidationReport("acme")
    check_env_example(settings.profile_chain, report)
    assert report.issues == []


def test_lenient_load_keeps_unset_variables_visible(profiles: Path) -> None:
    (profiles / "acme" / "profile.yaml").write_text(
        MINIMAL_PROFILE.replace('"http://localhost:8101/mcp"', '"${ACME_LOGS_URL}"')
    )
    settings, missing = load_settings_lenient("acme", config_of(profiles), keep_missing=True)
    assert missing == ["ACME_LOGS_URL"]
    assert settings.capability("logs").mcp.url == "${ACME_LOGS_URL}"
    with pytest.raises(ConfigError, match="ACME_LOGS_URL"):
        load_settings("acme", config_of(profiles))


# --------------------------------------------------------------------------- init


def test_init_copies_template_and_sets_metadata(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    shutil.copytree(REPO_PROFILES / "_template", profiles / "_template")
    (profiles / "_template" / ".env").write_text("OPENAI_COMPAT_API_KEY=leak\n")
    target = init_profile(profiles, "acme", company="Acme Corp", description='Acme "prod"')
    assert sorted(p.name for p in target.iterdir()) == [
        ".env.example",
        "profile.yaml",
        "services.yaml",
    ]  # no .env (secrets) and no template README
    text = (target / "profile.yaml").read_text()
    assert 'company: "Acme Corp"' in text and 'description: "Acme \\"prod\\""' in text
    assert "# COMPANY PROFILE TEMPLATE" in text  # comments survive
    (tmp_path / "config" / "prompts").mkdir(parents=True)
    settings, missing = load_settings_lenient("acme", tmp_path / "config", keep_missing=True)
    assert settings.metadata.company == "Acme Corp"
    assert settings.metadata.description == 'Acme "prod"'
    assert "OPENAI_COMPAT_API_KEY" in missing


def test_init_from_local_adds_metadata(tmp_path: Path) -> None:
    profiles = tmp_path / "profiles"
    shutil.copytree(REPO_PROFILES / "local", profiles / "local")
    target = init_profile(profiles, "acme-demo", "local")
    (tmp_path / "config" / "prompts").mkdir(parents=True)
    settings = load_settings("acme-demo", tmp_path / "config")
    assert settings.metadata.company == "acme-demo"
    assert (target / "services.yaml").is_file()


@pytest.mark.parametrize(
    ("name", "source", "message"),
    [
        ("Acme Corp", "_template", "Invalid profile name"),
        ("local", "_template", "already exists"),
        ("acme", "nope", "Source profile 'nope' not found"),
    ],
)
def test_init_errors(tmp_path: Path, name: str, source: str, message: str) -> None:
    profiles = tmp_path / "profiles"
    shutil.copytree(REPO_PROFILES / "_template", profiles / "_template")
    shutil.copytree(REPO_PROFILES / "local", profiles / "local")
    with pytest.raises(ConfigError, match=message):
        init_profile(profiles, name, source)


# --------------------------------------------------------------------------- show / diff


def test_mask_secrets() -> None:
    masked = mask_secrets(
        {"api_key": "sk-1", "mcp": {"env": {"GITHUB_TOKEN": "t", "URL": "u"}}, "pw": None}
    )
    assert masked == {
        "api_key": MASK,
        "mcp": {"env": {"GITHUB_TOKEN": MASK, "URL": "u"}},
        "pw": None,
    }
    assert mask_secrets({"api_key": "${KEY}"}) == {"api_key": "${KEY}"}


def test_resolved_dump_masks_llm_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "sk-super-secret")
    dumped = json.dumps(resolved_dump(load_settings("local", REPO_CONFIG)))
    assert "sk-super-secret" not in dumped


def test_diff_local_vs_local_k8s() -> None:
    dumps = []
    for name in ("local", "local-k8s"):
        settings = load_settings(name, REPO_CONFIG)
        dump = resolved_dump(settings)
        dump["catalog"] = catalog_summary(ServiceCatalog.from_settings(settings))
        dumps.append(dump)
    rows = {key: (a, b) for key, a, b in diff_dumps(dumps[0], dumps[1])}
    assert rows["capabilities.logs.settings.fields.service"] == ("service", "kubernetes.labels.app")
    assert rows["capabilities.logs.settings.service_filter"] == (None, True)
    key = "catalog.services.payment-service.logs.production.index_pattern"
    assert rows[key] == ("payment-prod-*", "logs-k8s-*")
    assert not any(k.startswith("capabilities.metrics") for k in rows)


# --------------------------------------------------------------------------- CLI

runner = CliRunner()


def test_cli_list_validate_show() -> None:
    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 0 and "local-k8s" in result.output and "_template" in result.output
    result = runner.invoke(app, ["profile", "validate", "--all"])
    assert result.exit_code == 0, result.output
    assert "Profile 'local': valid" in result.output
    result = runner.invoke(app, ["profile", "show", "local-k8s"])
    assert result.exit_code == 0 and "local-k8s -> local" in result.output
    result = runner.invoke(app, ["profile", "show", "local", "--resolved"])
    assert result.exit_code == 0 and '"environment": "local"' in result.output


def test_cli_validate_reports_errors_with_exit_code() -> None:
    result = runner.invoke(app, ["profile", "validate", "_template"])
    assert result.exit_code == 1
    assert "required variable OPENAI_COMPAT_API_KEY is not set" in result.output


def test_cli_diff_and_profile_option() -> None:
    result = runner.invoke(app, ["profile", "diff", "local", "local-k8s"])
    assert result.exit_code == 0 and "differences" in result.output
    result = runner.invoke(app, ["catalog", "list", "--profile", "local-k8s"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(app, ["catalog", "list", "--env", "local-k8s"])  # deprecated alias
    assert result.exit_code == 0, result.output


def test_cli_init(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    shutil.copytree(REPO_PROFILES / "_template", tmp_path / "_template")
    monkeypatch.setenv("AIOPS_PROFILES_DIR", str(tmp_path))
    result = runner.invoke(app, ["profile", "init", "acme", "--company", "Acme"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "acme" / "profile.yaml").is_file()
    assert "aiops profile validate acme" in result.output
    result = runner.invoke(app, ["profile", "init", "acme"])
    assert result.exit_code == 2 and "already exists" in result.output
