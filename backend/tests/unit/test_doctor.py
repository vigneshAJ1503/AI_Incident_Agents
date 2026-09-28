"""`aiops doctor` (PR-P3) against in-process fake MCP servers: zero network, zero tokens."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from typer.testing import CliRunner

from aiops.cli.main import app
from aiops.core.config import LLMConfig, Settings, load_settings_lenient
from aiops.core.doctor import Doctor, DoctorOptions, DoctorReport, Status, looks_like_write_tool
from aiops.core.profiles import ValidationReport
from aiops.llm.fake import FakeLLMProvider, text
from tests.conftest import write_config

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

PROFILE = """
llm:
  provider: openai_compat
  base_url: https://llm.example.com/v1
  api_key: ${TEST_LLM_KEY:-}
  models: {agent: some-model}
capabilities:
  logs:
    provider: elasticsearch
    mcp: {transport: http, url: "http://127.0.0.1:9/mcp", timeout_s: 1}
    tool_allowlist: [list_indices, search_logs]
  k8s:
    provider: kubernetes
    mcp: {transport: http, url: "http://127.0.0.1:9/mcp", timeout_s: 1}
    tool_allowlist: [list_deployments, get_deployment]
    settings: {default_namespace: prod}
"""

CATALOG = """
environments:
  production: {aliases: [prod]}
services:
  - name: payment-service
    capabilities:
      logs: {service_value: payment-service}
      k8s: {deployment: payment-service}
    environments:
      production:
        capabilities:
          logs: {index_pattern: "payment-prod-*"}
          k8s: {namespace: prod}
"""


def logs_server(indices: list[dict[str, Any]] | None = None) -> MCPServer:
    server = MCPServer("fake-logs")

    @server.tool()
    def list_indices() -> dict[str, Any]:
        """Indices."""
        return {
            "indices": indices
            if indices is not None
            else [{"index": "payment-prod-2026.09.28", "docs": 120}]
        }

    @server.tool()
    def search_logs(
        index: str,
        start: str,
        end: str,
        size: int = 20,
        query: str = "",
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        """Search."""
        return {"total": 7, "returned": 1, "hits": [{}]}

    return server


def k8s_server(
    *,
    deployments: tuple[str, ...] = ("payment-service",),
    write_tool: bool = False,
    drop: str | None = None,
) -> MCPServer:
    server = MCPServer("fake-k8s")

    if drop != "list_deployments":

        @server.tool()
        def list_deployments(namespace: str, limit: int = 50) -> dict[str, Any]:
            """Deployments."""
            if namespace != "prod":
                raise ToolError(f"namespace '{namespace}' is not allowed")
            return {"deployments": [{"name": d} for d in deployments]}

    @server.tool()
    def get_deployment(namespace: str, name: str, history: int = 5) -> dict[str, Any]:
        """One deployment."""
        if name not in deployments:
            raise ToolError(f"deployment '{name}' not found")
        return {"deployment": {"name": name, "replicas": {"desired": 2, "ready": 2}}}

    @server.tool()
    def list_pods(namespace: str) -> dict[str, Any]:
        """Pods (read-only, not allowlisted)."""
        return {"pods": []}

    if write_tool:

        @server.tool()
        def delete_pod(namespace: str, name: str) -> str:
            """Dangerous."""
            return "deleted"

    return server


def settings_for(
    tmp_path: Path, profile: str = PROFILE, catalog: str = CATALOG
) -> tuple[Settings, list[str]]:
    config = write_config(tmp_path, profile, catalog)
    return load_settings_lenient("test", config, keep_missing=True)


async def run_doctor(
    settings: Settings,
    overrides: dict[str, Any] | None = None,
    *,
    missing: list[str] | None = None,
    **options: Any,
) -> DoctorReport:
    options.setdefault("skip_llm", True)
    options.setdefault("timeout_s", 2.0)
    doctor = Doctor(
        settings,
        DoctorOptions(**options),
        missing_vars=missing,
        overrides=overrides,
        validation=ValidationReport(settings.profile),
        now=NOW,
    )
    return await doctor.run()


def find(report: DoctorReport, capability: str, check: str) -> list[Any]:
    return [c for c in report.checks if c.capability == capability and c.check == check]


async def test_everything_ok(tmp_path: Path) -> None:
    settings, _ = settings_for(tmp_path)
    report = await run_doctor(settings, {"logs": logs_server(), "k8s": k8s_server()})
    bad = [c for c in report.checks if c.status not in (Status.OK, Status.SKIP)]
    assert bad == []
    assert report.exit_code() == 0
    (reach,) = find(report, "logs", "reachability")
    assert reach.latency_ms is not None
    catalog = find(report, "logs", "catalog")
    assert "120 docs, 7 in the last hour" in catalog[0].detail
    (k8s_catalog,) = find(report, "k8s", "catalog")
    assert "prod/payment-service (2/2 ready)" in k8s_catalog.detail
    # Read-only tools the server has but agents aren't allowed are listed, not flagged.
    (contract,) = find(report, "k8s", "contract")
    assert "not allowlisted: list_pods" in contract.detail


async def test_missing_allowlisted_tool_fails(tmp_path: Path) -> None:
    settings, _ = settings_for(tmp_path)
    report = await run_doctor(
        settings, {"logs": logs_server(), "k8s": k8s_server(drop="list_deployments")}
    )
    (contract,) = find(report, "k8s", "contract")
    assert contract.status == Status.FAIL
    assert "missing on the server: list_deployments" in contract.detail
    assert report.exit_code() == 2


async def test_write_tools_on_the_server_warn(tmp_path: Path) -> None:
    settings, _ = settings_for(tmp_path)
    report = await run_doctor(settings, {"logs": logs_server(), "k8s": k8s_server(write_tool=True)})
    warns = [c for c in find(report, "k8s", "contract") if c.status == Status.WARN]
    assert len(warns) == 1
    assert "server exposes write tools: delete_pod" in warns[0].detail
    assert "read-only credential" in warns[0].hint
    assert report.exit_code() == 0
    assert report.exit_code(strict=True) == 1


async def test_write_tool_in_the_allowlist_fails(tmp_path: Path) -> None:
    profile = PROFILE.replace(
        "[list_deployments, get_deployment]", "[list_deployments, get_deployment, delete_pod]"
    )
    settings, _ = settings_for(tmp_path, profile)
    report = await run_doctor(
        settings, {"logs": logs_server(), "k8s": k8s_server(write_tool=True)}, capabilities=("k8s",)
    )
    fails = [c for c in find(report, "k8s", "contract") if c.status == Status.FAIL]
    assert any("write-looking tools: delete_pod" in c.detail for c in fails)


async def test_unreachable_server_fails_fast(tmp_path: Path) -> None:
    settings, _ = settings_for(tmp_path)
    report = await run_doctor(settings, {"k8s": k8s_server()}, timeout_s=1.0)
    (reach,) = find(report, "logs", "reachability")
    assert reach.status == Status.FAIL
    assert "cannot connect to http://127.0.0.1:9/mcp" in reach.detail
    assert "make mcp-up" in reach.hint
    # No contract/smoke rows for an unreachable server; others still run.
    assert find(report, "logs", "smoke") == []
    assert find(report, "k8s", "smoke") or find(report, "k8s", "catalog")
    assert report.exit_code() == 2


async def test_missing_required_variable_fails_without_printing_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TEST_LLM_KEY", "sk-do-not-print-me")
    profile = PROFILE.replace(
        '"http://127.0.0.1:9/mcp", timeout_s: 1}\n    tool_allowlist: [list_i',
        '"${LOGS_MCP_URL_REQUIRED}", timeout_s: 1}\n    tool_allowlist: [list_i',
    )
    settings, missing = settings_for(tmp_path, profile)
    assert missing == ["LOGS_MCP_URL_REQUIRED"]
    report = await run_doctor(
        settings, {"logs": logs_server(), "k8s": k8s_server()}, missing=missing
    )
    fails = [c for c in report.checks if c.status == Status.FAIL]
    assert [c.detail for c in fails] == ["required variable LOGS_MCP_URL_REQUIRED is not set"]
    assert "sk-do-not-print-me" not in json.dumps(report.as_dict())


async def test_catalog_misses_fail(tmp_path: Path) -> None:
    settings, _ = settings_for(tmp_path)
    report = await run_doctor(
        settings,
        {
            "logs": logs_server(indices=[{"index": "other-prod-2026.09.28", "docs": 5}]),
            "k8s": k8s_server(deployments=("something-else",)),
        },
    )
    (logs_miss,) = find(report, "logs", "catalog")
    assert logs_miss.status == Status.FAIL
    assert "'payment-prod-*' matches no readable index" in logs_miss.detail
    (k8s_miss,) = find(report, "k8s", "catalog")
    assert k8s_miss.status == Status.FAIL
    assert "deployment 'payment-service' not found in 'prod'" in k8s_miss.detail
    assert "catalog import" in k8s_miss.hint


async def test_unknown_service_and_capability(tmp_path: Path) -> None:
    settings, _ = settings_for(tmp_path)
    report = await run_doctor(
        settings,
        {"logs": logs_server(), "k8s": k8s_server()},
        services=("payments", "nope"),
        capabilities=("k8s",),
    )
    assert [c.capability for c in report.checks if c.capability not in ("profile", "llm")] == [
        "k8s"
    ] * len(
        find(report, "k8s", "config")
        + find(report, "k8s", "reachability")
        + find(report, "k8s", "contract")
        + find(report, "k8s", "smoke")
        + find(report, "k8s", "catalog")
    )
    assert any("service 'nope' is not in the catalog" in c.detail for c in report.checks)
    with pytest.raises(Exception, match="not enabled"):
        await run_doctor(settings, {}, capabilities=("metrics",))


async def test_smoke_tool_not_allowlisted_is_skipped(tmp_path: Path) -> None:
    profile = PROFILE.replace("[list_indices, search_logs]", "[search_logs]")
    settings, _ = settings_for(tmp_path, profile)
    report = await run_doctor(settings, {"logs": logs_server()}, capabilities=("logs",))
    (smoke,) = find(report, "logs", "smoke")
    assert smoke.status == Status.SKIP
    assert "'list_indices' is not in tool_allowlist" in smoke.detail


async def test_llm_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings, _ = settings_for(tmp_path)
    no_key = await run_doctor(
        settings, {"logs": logs_server(), "k8s": k8s_server()}, skip_llm=False
    )
    (llm,) = find(no_key, "llm", "llm")
    assert llm.status == Status.WARN
    assert "agents will fail until an LLM key is set" in llm.detail

    monkeypatch.setenv("TEST_LLM_KEY", "sk-secret-value")
    settings, _ = settings_for(tmp_path / "2")
    seen: list[LLMConfig] = []

    def factory(config: LLMConfig) -> FakeLLMProvider:
        seen.append(config)
        return FakeLLMProvider([text("pong")])

    doctor = Doctor(
        settings,
        DoctorOptions(capabilities=("logs",)),
        overrides={"logs": logs_server()},
        llm_factory=factory,
        validation=ValidationReport("test"),
    )
    report = await doctor.run()
    (llm,) = find(report, "llm", "llm")
    assert llm.status == Status.OK
    assert seen[0].max_retries == 0  # one ping, no retry storm

    def broken(config: LLMConfig) -> FakeLLMProvider:
        raise RuntimeError("401 invalid key sk-secret-value")

    doctor = Doctor(
        settings,
        DoctorOptions(capabilities=("logs",)),
        overrides={"logs": logs_server()},
        llm_factory=broken,
        validation=ValidationReport("test"),
    )
    (llm,) = find(await doctor.run(), "llm", "llm")
    assert llm.status == Status.FAIL
    assert "sk-secret-value" not in llm.detail


async def test_llm_check_shows_provider_and_model_per_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-P4b: the real Anthropic adapter over a mocked transport; bedrock without region."""
    from tests.unit import llm_wire as w

    anthropic_profile = PROFILE.replace(
        "provider: openai_compat\n  base_url: https://llm.example.com/v1\n"
        "  api_key: ${TEST_LLM_KEY:-}\n  models: {agent: some-model}",
        "provider: anthropic\n  api_key: ${TEST_ANTHROPIC_KEY:-}\n"
        "  models: {fast: small-claude, agent: big-claude}",
    )
    assert "anthropic" in anthropic_profile
    monkeypatch.setenv("TEST_ANTHROPIC_KEY", w.ANTHROPIC_KEY)
    settings, _ = settings_for(tmp_path, anthropic_profile)

    def factory(config: LLMConfig) -> Any:
        provider, _ = w.anthropic_server(lambda m, t: text("pong"), None, config)
        return provider

    doctor = Doctor(
        settings,
        DoctorOptions(capabilities=("logs",)),
        overrides={"logs": logs_server()},
        llm_factory=factory,
        validation=ValidationReport("test"),
    )
    (llm,) = find(await doctor.run(), "llm", "llm")
    assert llm.status == Status.OK, llm.detail
    assert llm.detail.startswith("anthropic (api.anthropic.com) fast=small-claude")
    assert "agent=big-claude rca=big-claude" in llm.detail
    assert w.ANTHROPIC_KEY not in llm.detail

    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-aws-config"))
    bedrock_profile = anthropic_profile.replace(
        "provider: anthropic\n  api_key: ${TEST_ANTHROPIC_KEY:-}", "provider: bedrock"
    )
    settings, _ = settings_for(tmp_path / "bedrock", bedrock_profile)
    report = await run_doctor(
        settings, {"logs": logs_server(), "k8s": k8s_server()}, skip_llm=False
    )
    (llm,) = find(report, "llm", "llm")
    assert llm.status == Status.FAIL
    assert "bedrock (bedrock-runtime no region)" in llm.detail and "AWS_REGION" in llm.detail


def test_write_tool_heuristic() -> None:
    for name in (
        "jira_create_issue",
        "delete_pod",
        "exec_in_pod",
        "patchDeployment",
        "scale",
        "create_silence",
        "jira_add_comment",
        "rollout-restart",
    ):
        assert looks_like_write_tool(name), name
    for name in (
        "list_silences",
        "execute_esql",
        "get_alert_groups",
        "search_commits",
        "get_diff",
        "list_deployments",
        "query_range",
        "jira_search",
        "list_docs",
    ):
        assert not looks_like_write_tool(name), name


def test_cli_json_and_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = write_config(tmp_path, PROFILE, CATALOG)
    monkeypatch.setenv("AIOPS_CONFIG_DIR", str(config))
    result = CliRunner().invoke(
        app, ["doctor", "--profile", "test", "--json", "--skip-llm", "--timeout", "1", "-c", "logs"]
    )
    assert result.exit_code == 2, result.output
    data = json.loads(result.output)
    assert data["exit_code"] == 2
    statuses = {(c["capability"], c["check"]): c["status"] for c in data["checks"]}
    assert statuses[("logs", "reachability")] == "FAIL"
    assert statuses[("llm", "llm")] == "SKIP"


LOKI_PROFILE = """
llm: {provider: fake}
capabilities:
  logs:
    provider: loki
    mcp: {transport: http, url: "http://127.0.0.1:9/mcp", timeout_s: 1}
    tool_allowlist: [query, query_range, list_labels]
    settings:
      stream_labels: [namespace, app, level]
      fields: {service: app}
      service_filter: true
"""

LOKI_CATALOG = """
environments:
  production: {aliases: [prod]}
services:
  - name: payment-service
    environments:
      production: {capabilities: {logs: {index_pattern: '{namespace="prod"}'}}}
  - name: order-service
    environments:
      production: {capabilities: {logs: {index_pattern: '{namespace="prod"}'}}}
"""


def loki_server(seen: list[str]) -> MCPServer:
    server = MCPServer("fake-loki")

    @server.tool()
    def list_labels(start: str | None = None, end: str | None = None) -> dict[str, Any]:
        """Labels."""
        return {"labels": ["app", "level", "namespace"]}

    @server.tool()
    def query(query: str, time: str | None = None) -> dict[str, Any]:
        """Instant metric query."""
        seen.append(query)
        if "order-service" in query:
            return {"series": []}
        return {"series": [{"labels": {}, "value": 42}]}

    @server.tool()
    def query_range(query: str, start: str, end: str, limit: int = 100) -> dict[str, Any]:
        """Range query."""
        return {"lines": []}

    return server


async def test_loki_smoke_and_catalog(tmp_path: Path) -> None:
    settings, _ = settings_for(tmp_path, LOKI_PROFILE, LOKI_CATALOG)
    seen: list[str] = []
    report = await run_doctor(settings, {"logs": loki_server(seen)}, capabilities=("logs",))
    (smoke,) = find(report, "logs", "smoke")
    assert smoke.status is Status.OK and "3 stream labels" in smoke.detail
    catalog = {c.detail.split(":")[0]: c for c in find(report, "logs", "catalog")}
    assert catalog["payment-service"].status is Status.OK
    assert catalog["payment-service"].detail.endswith("= 42")
    assert catalog["order-service"].status is Status.WARN
    assert 'sum(count_over_time({namespace="prod", app="payment-service"} [1h]))' in seen
