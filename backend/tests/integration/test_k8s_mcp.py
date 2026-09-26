"""Requires Minikube (make k8s-up) and kubernetes-mcp (make kubernetes-mcp-up).

Proves the PR-018 contract live: the agent-facing toolset has only read tools, the
server enforces its namespace allowlist, and a write is denied by BOTH layers:
  1. the capability allowlist (and the server has no write tool at all), and
  2. Kubernetes RBAC for the aiops-reader ServiceAccount (kubectl auth can-i, and a
     real DELETE with the ServiceAccount's own token -> 403).
Run with: make test-integration
"""

from __future__ import annotations

import base64
import shutil
import ssl
import subprocess
from pathlib import Path

import httpx2
import pytest
import yaml

from aiops.core.config import load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.mcp.client import MCPClient
from aiops.mcp.registry import MCPRegistry

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[3]
CONFIG = ROOT / "config"
KUBECONFIG = ROOT / ".data" / "k8s" / "aiops-reader.kubeconfig"
SA = "system:serviceaccount:aiops-system:aiops-reader"
CONTEXT = "aiops"
READ_TOOLS = {
    "list_namespaces",
    "list_pods",
    "get_pod",
    "list_events",
    "list_deployments",
    "get_deployment",
    "get_pod_logs",
    "list_services",
}
FORBIDDEN = [
    ("delete", "pods"),
    ("create", "pods"),
    ("patch", "deployments.apps"),
    ("update", "deployments.apps/scale"),
    ("delete", "deployments.apps"),
    ("create", "pods/exec"),
    ("create", "pods/portforward"),
    ("get", "secrets"),
    ("list", "secrets"),
    ("get", "configmaps"),
    ("list", "configmaps"),
    ("create", "serviceaccounts/token"),
]
ALLOWED = [("list", "pods"), ("get", "pods/log"), ("list", "events"), ("get", "deployments.apps")]


def kubectl(*args: str) -> subprocess.CompletedProcess[str]:
    if shutil.which("kubectl") is None:
        pytest.skip("kubectl not installed")
    return subprocess.run(  # noqa: S603 - fixed argv
        ["kubectl", "--context", CONTEXT, *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def can_i(verb: str, resource: str) -> str:
    return kubectl("auth", "can-i", verb, resource, "-n", "prod", f"--as={SA}").stdout.strip()


async def test_toolset_is_read_only_and_namespace_guarded() -> None:
    settings = load_settings("local", CONFIG)
    audit = MemoryAuditSink()
    async with MCPRegistry(settings, audit=audit).toolset("k8s", agent="it") as tools:
        assert {s.name for s in await tools.specs()} == READ_TOOLS

        pods = await tools.call(
            "list_pods", {"namespace": "prod", "label_selector": "app=postgres"}
        )
        assert pods.ok and pods.data["pods"][0]["name"].startswith(
            "postgres-"
        )  # no fault touches it

        dep = await tools.call("get_deployment", {"namespace": "prod", "name": "payment-service"})
        assert dep.ok and dep.data["rollout_history"][0]["current"] is True
        assert "last-applied-configuration" not in dep.text

        other = await tools.call("list_pods", {"namespace": "kube-system"})
        assert not other.ok and "not allowed" in other.content

        # Layer 1: a write tool is blocked by the capability allowlist before any call.
        write = await tools.call("delete_pod", {"namespace": "prod", "name": "x"})
        assert not write.ok and write.tool_call.status == "blocked"


async def test_server_exposes_no_write_tools() -> None:
    settings = load_settings("local", CONFIG)
    async with MCPClient.from_config("k8s", settings.capability("k8s").mcp) as client:
        names = {t.name for t in await client.list_tools()}
    assert names == READ_TOOLS  # nothing to allowlist by mistake


def test_rbac_denies_writes_secrets_and_exec() -> None:
    kubectl("apply", "-k", str(ROOT / "deploy" / "k8s" / "rbac"))
    for verb, resource in FORBIDDEN:
        assert can_i(verb, resource) == "no", (verb, resource)
    for verb, resource in ALLOWED:
        assert can_i(verb, resource) == "yes", (verb, resource)
    assert (
        kubectl("auth", "can-i", "list", "pods", "-n", "kube-system", f"--as={SA}").stdout.strip()
        == "no"
    )


def test_real_delete_with_the_reader_token_is_forbidden() -> None:
    """Layer 2 end to end: the ServiceAccount's own token gets 403 on a DELETE."""
    if not KUBECONFIG.is_file():
        pytest.skip("run make k8s-reader-kubeconfig first")
    config = yaml.safe_load(KUBECONFIG.read_text())
    token = config["users"][0]["user"]["token"]
    ca = base64.b64decode(config["clusters"][0]["cluster"]["certificate-authority-data"]).decode()
    server = kubectl(
        "config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}"
    ).stdout.strip()  # host-reachable URL (the kubeconfig's points at the node IP)
    context = ssl.create_default_context(cadata=ca)
    with httpx2.Client(base_url=server, verify=context, timeout=15) as http:
        headers = {"Authorization": f"Bearer {token}"}
        ok = http.get("/api/v1/namespaces/prod/pods", headers=headers, params={"limit": 1})
        assert ok.status_code == 200
        denied = http.delete("/api/v1/namespaces/prod/pods/does-not-exist", headers=headers)
        assert denied.status_code == 403, denied.text
        secret = http.get("/api/v1/namespaces/prod/secrets/app-db", headers=headers)
        assert secret.status_code == 403
