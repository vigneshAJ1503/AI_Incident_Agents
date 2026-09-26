"""`extends:` for environments and service catalogs (PR-016: local-k8s = local + real logs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aiops.core.catalog import ServiceCatalog, merge_catalogs
from aiops.core.config import ConfigError, deep_merge, load_settings
from tests.conftest import write_config

BASE = """
environment: base
llm:
  provider: fake
capabilities:
  logs:
    provider: elasticsearch
    mcp: {transport: http, url: "http://localhost:8101/mcp"}
    tool_allowlist: [search_logs, execute_esql]
    settings:
      fields: {timestamp: "@timestamp", service: service}
      baseline_hours: 24
"""

CHILD = """
extends: test
service_catalog: child
capabilities:
  logs:
    settings:
      fields: {service: kubernetes.labels.app}
      service_filter: true
"""

CATALOG = """
environments: {production: {aliases: [prod]}}
services:
  - name: payment-service
    aliases: [pay]
    capabilities:
      logs: {service_value: payment-service}
    environments:
      production: {capabilities: {logs: {index_pattern: "payment-prod-*"}, k8s: {namespace: prod}}}
  - name: order-service
    capabilities:
      logs: {service_value: order-service}
"""

CHILD_CATALOG = """
extends: local
services:
  - name: payment-service
    environments:
      production: {capabilities: {logs: {index_pattern: "logs-k8s-*"}}}
  - name: new-service
    description: only in the child
"""


def write_child(tmp_path: Path) -> Path:
    config = write_config(tmp_path, BASE, CATALOG)
    (config / "environments" / "child.yaml").write_text(CHILD)
    (config / "service-catalog" / "child.yaml").write_text(CHILD_CATALOG)
    return config


def test_deep_merge_merges_mappings_and_replaces_the_rest() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    assert deep_merge(base, {"a": {"y": [3]}, "c": 2}) == {"a": {"x": 1, "y": [3]}, "b": 1, "c": 2}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1}  # inputs untouched


def test_environment_extends_overrides_only_what_differs(tmp_path: Path) -> None:
    settings = load_settings("child", write_child(tmp_path))
    assert settings.environment == "child"  # the parent's name is never inherited
    assert settings.service_catalog == "child"
    logs = settings.capability("logs")
    assert logs.tool_allowlist == ["search_logs", "execute_esql"]
    assert logs.settings["fields"] == {
        "timestamp": "@timestamp",
        "service": "kubernetes.labels.app",
    }
    assert logs.settings["service_filter"] is True
    assert logs.settings["baseline_hours"] == 24
    # The parent still loads unchanged.
    assert (
        "service_filter"
        not in load_settings("test", tmp_path / "config").capability("logs").settings
    )


def test_catalog_extends_merges_services_by_name(tmp_path: Path) -> None:
    catalog = ServiceCatalog.from_settings(load_settings("child", write_child(tmp_path)))
    payment = catalog.get("payment-service")
    assert payment.aliases == ["pay"]
    assert payment.identifiers("logs", "production") == {
        "service_value": "payment-service",
        "index_pattern": "logs-k8s-*",
    }
    assert payment.identifiers("k8s", "production") == {"namespace": "prod"}
    assert catalog.get("order-service").identifiers("logs") == {"service_value": "order-service"}
    assert catalog.get("new-service").description == "only in the child"
    assert catalog.resolve_environment("prod") == "production"


def test_merge_catalogs_keeps_order_and_appends() -> None:
    merged = merge_catalogs(
        {"services": [{"name": "a", "x": 1}, {"name": "b"}]},
        {"services": [{"name": "b", "y": 2}, {"name": "c"}]},
    )
    assert merged["services"] == [{"name": "a", "x": 1}, {"name": "b", "y": 2}, {"name": "c"}]


def test_circular_extends_is_a_config_error(tmp_path: Path) -> None:
    config = write_config(tmp_path, "extends: loop\n")
    (config / "environments" / "loop.yaml").write_text("extends: test\n")
    with pytest.raises(ConfigError, match="Circular 'extends'"):
        load_settings("test", config)


def test_missing_parent_is_a_config_error(tmp_path: Path) -> None:
    config = write_config(tmp_path, "extends: nope\n")
    with pytest.raises(ConfigError, match=r"nope\.yaml"):
        load_settings("test", config)


def test_repo_local_k8s_environment(repo_config_dir: Path) -> None:
    """local-k8s = local with real Kubernetes logs (PR-016); everything else is inherited."""
    local = load_settings("local", repo_config_dir)
    k8s = load_settings("local-k8s", repo_config_dir)
    assert k8s.environment == "local-k8s" and k8s.service_catalog == "local-k8s"
    assert set(k8s.capabilities) == set(local.capabilities)
    logs = k8s.capability("logs").settings
    assert logs["service_filter"] is True
    assert logs["fields"]["service"] == "kubernetes.labels.app"
    assert logs["fields"]["message_keyword"] == "message.keyword"
    assert k8s.agents == local.agents

    catalog = ServiceCatalog.from_settings(k8s)
    for service in catalog.services:
        ids = service.identifiers("logs", "production")
        assert ids["index_pattern"] == "logs-k8s-*"
        assert ids["service_value"] == service.name
    local_catalog = ServiceCatalog.from_settings(local)
    assert [s.name for s in catalog.services] == [s.name for s in local_catalog.services]
    assert (
        local_catalog.get("payment-service").identifiers("logs", "production")["index_pattern"]
        == "payment-prod-*"
    )
