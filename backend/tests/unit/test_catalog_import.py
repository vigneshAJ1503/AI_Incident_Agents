"""`aiops catalog import` (PR-P3): Kubernetes + Backstage mapping, merge semantics, writing."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from mcp.server.mcpserver import MCPServer
from typer.testing import CliRunner

from aiops.cli.main import app
from aiops.core.catalog import ServiceCatalog
from aiops.core.catalog_import import (
    DEFAULT_K8S_IMPORT,
    MappingOptions,
    apply_plan,
    derive_aliases,
    fetch_backstage_api,
    fetch_k8s_deployments,
    load_backstage_dir,
    plan_import,
    render_plan,
    service_from_backstage,
    service_from_deployment,
    validate_catalog_text,
)
from aiops.core.config import CapabilityConfig, ConfigError, GuardrailsConfig, load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.mcp.client import MCPClient
from aiops.mcp.toolset import Toolset
from tests.conftest import REPO_ROOT

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "backstage"
OPTS = MappingOptions(environment="production")


def deployment(name: str, **extra: Any) -> dict[str, Any]:
    """A compact kubernetes-mcp deployment, as get_deployment returns it."""
    return {
        "name": name,
        "namespace": "prod",
        "labels": {"app": name, "team": "payments"},
        "selector": {"app": name},
        "images": [{"container": "app", "image": "x:1"}],
        **extra,
    }


# --------------------------------------------------------------------------- kubernetes


def test_deployment_maps_to_a_catalog_entry() -> None:
    entry = service_from_deployment(deployment("payment-service"), OPTS)
    assert entry == {
        "name": "payment-service",
        "aliases": ["payment"],
        "owners": {"team": "payments"},
        "capabilities": {
            "logs": {"service_value": "payment-service"},
            "metrics": {"labels": {"service": "payment-service"}},
            "alerts": {"labels": {"service": "payment-service"}},
            "k8s": {
                "deployment": "payment-service",
                "label_selector": "app=payment-service",
                "container": "app",
            },
        },
        "environments": {
            "production": {
                "capabilities": {
                    "k8s": {"namespace": "prod"},
                    "metrics": {"labels": {"service": "payment-service", "namespace": "prod"}},
                }
            }
        },
    }


def test_deployment_name_and_team_fallbacks() -> None:
    dep = deployment(
        "pay-v2",
        labels={"app.kubernetes.io/name": "payments", "app.kubernetes.io/part-of": "fintech"},
        images=[{"container": "sidecar", "image": "a"}, {"container": "pay-v2", "image": "b"}],
    )
    entry = service_from_deployment(dep, OPTS)
    assert entry["name"] == "payments"
    assert entry["owners"] == {"team": "fintech"}
    assert entry["capabilities"]["k8s"]["container"] == "pay-v2"  # the one named like it
    assert entry["capabilities"]["k8s"]["deployment"] == "pay-v2"

    bare = deployment("Legacy_Billing", labels={}, owner_annotations={"acme.io/team": "billing"})
    entry = service_from_deployment(bare, MappingOptions())
    assert entry["name"] == "legacy-billing"
    assert entry["owners"] == {"team": "billing"}  # from the ownership annotation
    assert "production" not in entry["environments"]  # no environment: the namespace
    assert entry["environments"]["prod"]["capabilities"]["k8s"] == {"namespace": "prod"}


def test_label_names_and_rules_come_from_the_profile(tmp_path: Path) -> None:
    settings = load_settings("local", REPO_ROOT / "config")
    k8s = settings.capabilities["k8s"]
    custom = k8s.model_copy(
        update={"settings": {**k8s.settings, "catalog_import": {"team_labels": ["squad"]}}}
    )
    metrics = settings.capabilities["metrics"]
    job = metrics.model_copy(
        update={"settings": {**metrics.settings, "labels": {"service": "job", "namespace": "ns"}}}
    )
    settings = settings.model_copy(
        update={"capabilities": {**settings.capabilities, "k8s": custom, "metrics": job}}
    )
    opts = MappingOptions.from_settings(settings, "production")
    assert opts.k8s_import["team_labels"] == ["squad"]
    assert opts.k8s_import["name_labels"] == DEFAULT_K8S_IMPORT["name_labels"]
    entry = service_from_deployment(
        deployment("x-svc", labels={"app": "x-svc", "squad": "s"}), opts
    )
    assert entry["owners"] == {"team": "s"}
    assert entry["capabilities"]["metrics"] == {"labels": {"job": "x-svc"}}
    env = entry["environments"]["production"]["capabilities"]
    assert env["metrics"] == {"labels": {"job": "x-svc", "ns": "prod"}}


def test_aliases() -> None:
    assert derive_aliases("payment-service") == ["payment"]
    assert derive_aliases("orders-api") == ["orders"]
    assert derive_aliases("redis") == []
    assert derive_aliases("-service") == []


def fake_k8s() -> MCPServer:
    server = MCPServer("fake-k8s")
    deps = {"payment-service": deployment("payment-service"), "redis": deployment("redis")}

    @server.tool()
    def list_deployments(
        namespace: str, label_selector: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """List (no selector, like kubernetes-mcp's compact list)."""
        names = sorted(deps)
        if label_selector == "team!=platform":
            names = ["payment-service"]
        return {"deployments": [{"name": n, "labels": deps[n]["labels"]} for n in names]}

    @server.tool()
    def get_deployment(namespace: str, name: str, history: int = 5) -> dict[str, Any]:
        """Detail with selector."""
        return {"deployment": deps[name], "rollout_history": []}

    return server


async def test_fetch_k8s_deployments_through_the_guarded_toolset() -> None:
    config = CapabilityConfig.model_validate(
        {
            "provider": "kubernetes",
            "mcp": {"transport": "http", "url": "http://unused"},
            "tool_allowlist": ["list_deployments", "get_deployment"],
        }
    )
    audit = MemoryAuditSink()
    async with MCPClient("k8s", fake_k8s()) as client:
        toolset = Toolset(
            "k8s",
            config,
            client,
            agent="catalog-import",
            guardrails=GuardrailsConfig(),
            audit=audit,
        )
        found = await fetch_k8s_deployments(toolset, ["prod"], label_selector="team!=platform")
    assert [d["name"] for d in found] == ["payment-service"]
    assert found[0]["selector"] == {"app": "payment-service"}
    assert [r.tool_call.tool for r in audit.records] == ["list_deployments", "get_deployment"]


# --------------------------------------------------------------------------- backstage


def test_backstage_directory_maps_components() -> None:
    entities = load_backstage_dir(FIXTURES)
    assert sorted(e["metadata"]["name"] for e in entities) == [
        "checkout-service",
        "order-service",
        "payment-service",
    ]  # the Group is ignored
    by_name = {e["name"]: e for e in (service_from_backstage(x, OPTS) for x in entities)}
    payment = by_name["payment-service"]
    assert payment["description"] == "Processes card payments for orders."
    assert payment["aliases"] == ["payment", "payments-api"]  # derived + title
    assert payment["owners"] == {"team": "payments"}
    assert payment["depends_on"] == ["user-service", "postgres", "redis"]
    caps = payment["capabilities"]
    assert caps["k8s"] == {
        "deployment": "payment-service",
        "label_selector": "backstage.io/kubernetes-id=payment-service",
        "namespace": "prod",
    }
    assert caps["code"] == {"repo": "payment-service"}
    assert caps["tickets"] == {"project_key": "PAY", "components": ["payments"]}
    assert caps["alerts"] == {
        "labels": {"service": "payment-service"},
        "pagerduty_service_id": "PXXXXXX",
    }
    order = by_name["order-service"]
    assert order["capabilities"]["k8s"] == {"label_selector": "app=order-service"}
    assert order["capabilities"]["code"] == {"repo": "order-service"}  # gitlab slug
    assert order["owners"] == {"team": "commerce"}
    assert by_name["checkout-service"]["owners"] == {"team": "jane.doe"}


def test_backstage_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_backstage_dir(tmp_path / "nope")
    (tmp_path / "catalog-info.yaml").write_text("kind: Group\nmetadata: {name: g}\n")
    with pytest.raises(ConfigError, match="No Backstage"):
        load_backstage_dir(tmp_path)


def test_backstage_api_is_read_only_and_sends_the_token() -> None:
    seen: list[httpx2.Request] = []
    entity = yaml.safe_load(
        (FIXTURES / "commerce" / "catalog-info.yaml").read_text().split("---")[0]
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"items": [entity, {"kind": "Group"}]})

    items = fetch_backstage_api(
        "https://backstage.example.com/", "tok-123", transport=httpx2.MockTransport(handler)
    )
    assert [i["metadata"]["name"] for i in items] == ["order-service"]
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/catalog/entities"
    assert seen[0].url.params["filter"] == "kind=component"
    assert seen[0].headers["authorization"] == "Bearer tok-123"

    def denied(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, json={})

    with pytest.raises(ConfigError, match="HTTP 401"):
        fetch_backstage_api("https://b.example.com", None, transport=httpx2.MockTransport(denied))


# --------------------------------------------------------------------------- merge


EXISTING = {
    "environments": {"production": {"aliases": ["prod"]}},
    "services": [
        {
            "name": "payment-service",
            "aliases": ["payment", "pay"],
            "owners": {"team": "payments-core"},  # hand-edited: must survive
            "capabilities": {"k8s": {"deployment": "payment-service", "container": "main"}},
        },
        {"name": "order-service", "aliases": ["checkout"]},
    ],
}


def test_merge_never_overwrites_and_only_adds() -> None:
    imported = [
        service_from_deployment(deployment("payment-service"), OPTS),
        {**service_from_deployment(deployment("checkout"), OPTS), "aliases": ["checkout"]},
    ]
    plan = plan_import(EXISTING, imported)
    payment, checkout = plan.changes
    assert payment.kind == "update"
    assert ("owners.team", "payments-core", "payments") in payment.kept
    assert ("capabilities.k8s.container", "main", "app") in payment.kept
    assert "owners" not in payment.added
    assert payment.added["capabilities"]["k8s"] == {"label_selector": "app=payment-service"}
    assert "aliases" not in payment.added  # 'payment' is already there
    assert checkout.kind == "new"
    assert "aliases" not in checkout.added  # 'checkout' belongs to order-service
    text = "\n".join(render_plan(plan))
    assert "= owners.team: kept payments-core (import: payments)" in text
    assert "+ service checkout (new)" in text


def test_merge_is_idempotent_and_lists_union() -> None:
    first = plan_import(EXISTING, [{"name": "order-service", "depends_on": ["payment-service"]}])
    assert first.changes[0].added == {"depends_on": ["payment-service"]}
    merged = {
        **EXISTING,
        "services": [
            EXISTING["services"][0],
            {"name": "order-service", "aliases": ["checkout"], "depends_on": ["payment-service"]},
        ],
    }
    again = plan_import(
        merged, [{"name": "order-service", "depends_on": ["payment-service", "db"]}]
    )
    assert again.changes[0].added == {"depends_on": ["payment-service", "db"]}
    same = plan_import(merged, [{"name": "order-service", "depends_on": ["payment-service"]}])
    assert same.changes[0].kind == "unchanged"
    assert not same.has_changes


def test_replace_mode_replaces_services() -> None:
    plan = plan_import(
        EXISTING, [{"name": "payment-service", "owners": {"team": "x"}}], mode="replace"
    )
    assert [(c.name, c.kind) for c in plan.changes] == [("payment-service", "update")]
    assert plan.changes[0].added == {"owners": {"team": "x"}}


CATALOG_TEXT = """\
# Hand-written catalog: this comment must survive an import.
environments:
  production:
    aliases: [prod]   # what people type

services:
  - name: payment-service
    aliases: [payment]
    owners: { team: payments-core }   # hand-edited, keep
    capabilities:
      k8s: { deployment: payment-service }
"""


def test_apply_preserves_comments_and_validates(tmp_path: Path) -> None:
    target = tmp_path / "services.yaml"
    target.write_text(CATALOG_TEXT)
    existing = yaml.safe_load(CATALOG_TEXT)
    imported = [
        service_from_deployment(deployment("payment-service"), OPTS),
        service_from_deployment(deployment("user-service", labels={"app": "user-service"}), OPTS),
    ]
    plan = plan_import(existing, imported)
    text = apply_plan(target, plan)
    assert "# Hand-written catalog: this comment must survive an import." in text
    assert "# hand-edited, keep" in text
    catalog = validate_catalog_text(target, text)
    payment = catalog.get("payment-service")
    assert payment.owners == {"team": "payments-core"}
    assert payment.identifiers("k8s", "production") == {
        "deployment": "payment-service",
        "label_selector": "app=payment-service",
        "container": "app",
        "namespace": "prod",
    }
    assert catalog.get("user-service").owners == {}
    # Re-planning against the written result: nothing left to add.
    assert not plan_import(yaml.safe_load(text), imported).has_changes


def test_apply_into_a_child_profile_with_extends(tmp_path: Path) -> None:
    parent = tmp_path / "base" / "services.yaml"
    parent.parent.mkdir()
    parent.write_text(CATALOG_TEXT)
    child = tmp_path / "child" / "services.yaml"
    plan = plan_import(
        yaml.safe_load(CATALOG_TEXT),
        [{"name": "payment-service", "aliases": ["payment", "payments"]}],
    )
    text = apply_plan(child, plan, extends="base")
    data = yaml.safe_load(text)
    assert data["extends"] == "base"
    # The FULL list is written: extends replaces lists, it doesn't append them.
    assert data["services"] == [{"name": "payment-service", "aliases": ["payment", "payments"]}]
    catalog = validate_catalog_text(child, text)
    assert catalog.get("payment-service").owners == {"team": "payments-core"}  # inherited


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def local_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profiles = tmp_path / "profiles"
    shutil.copytree(REPO_ROOT / "profiles" / "local", profiles / "local")
    monkeypatch.setenv("AIOPS_CONFIG_DIR", str(REPO_ROOT / "config"))
    monkeypatch.setenv("AIOPS_PROFILES_DIR", str(profiles))
    return profiles / "local" / "services.yaml"


def test_cli_backstage_dry_run_then_merge(local_copy: Path) -> None:
    before = local_copy.read_text()
    args = [
        "catalog",
        "import",
        "--from",
        "backstage",
        "--profile",
        "local",
        "--path",
        str(FIXTURES),
    ]
    runner = CliRunner()
    dry = runner.invoke(app, [*args, "--dry-run"], env={"COLUMNS": "200"})
    assert dry.exit_code == 0, dry.output
    assert "+ service checkout-service (new)" in dry.output
    assert "--dry-run: nothing written" in dry.output
    assert local_copy.read_text() == before

    wrote = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert wrote.exit_code == 0, wrote.output
    after = local_copy.read_text()
    assert after.startswith(before.splitlines()[0])  # the header comment survives
    catalog = ServiceCatalog.from_file(local_copy)
    assert catalog.get("checkout-service").identifiers("k8s")["deployment"] == "checkout"
    payment = catalog.get("payment-service")
    assert payment.identifiers("code") == {  # hand-written repo kept
        "repo": "sample-repo",
        "paths": ["services/payment-service"],
    }
    assert payment.identifiers("tickets")["project_key"] == "PAY"  # new identifier added

    again = runner.invoke(app, args, env={"COLUMNS": "200"})
    assert "Nothing to change." in again.output
    assert local_copy.read_text() == after


def test_cli_requires_a_source_location(local_copy: Path) -> None:
    result = CliRunner().invoke(app, ["catalog", "import", "--from", "backstage"])
    assert result.exit_code != 0
