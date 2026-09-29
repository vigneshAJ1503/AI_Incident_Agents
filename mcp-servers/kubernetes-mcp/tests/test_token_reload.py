"""A rotated ServiceAccount token is picked up without restarting the server.

The live-demo bug: after a Minikube restart the reader token was invalid; regenerating
the kubeconfig (scripts/k8s-reader-kubeconfig.sh, an atomic tmp + rename) was not seen by
the running server. These tests use a REAL kubeconfig file and the default loader.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import httpx2
import pytest

from kubernetes_mcp.kube import KubeClient, KubeError
from tests.fake_api import handler
from tests.test_server import SETTINGS

KUBECONFIG = """\
apiVersion: v1
kind: Config
clusters:
  - name: aiops
    cluster: {{server: "https://k8s.test"}}
users:
  - name: aiops-reader
    user: {{token: {token}}}
contexts:
  - name: reader
    context: {{cluster: aiops, user: aiops-reader, namespace: prod}}
current-context: reader
"""


def write_atomically(path: Path, token: str) -> None:
    """What the script does: write a temp file, then rename it over the old one."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(KUBECONFIG.format(token=token))
    tmp.replace(path)


class Cluster:
    """Fake API server that accepts exactly one (current) token."""

    def __init__(self, valid: str) -> None:
        self.valid = valid
        self.tokens: list[str] = []
        self.ok = handler([])

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        token = request.headers["Authorization"].removeprefix("Bearer ")
        self.tokens.append(token)
        if token != self.valid:
            return httpx2.Response(401, json={"message": "Unauthorized"})
        return self.ok(request)  # type: ignore[no-any-return]


def client_for(path: Path, cluster: Cluster) -> KubeClient:
    settings = replace(SETTINGS, kubeconfig=str(path))
    return KubeClient(settings, transport=httpx2.MockTransport(cluster))


async def test_rewritten_kubeconfig_is_used_before_any_401(tmp_path: Path) -> None:
    path = tmp_path / "aiops-reader.kubeconfig"
    write_atomically(path, "token-before-restart")
    cluster = Cluster(valid="token-before-restart")
    client = client_for(path, cluster)
    assert (await client.get_json("/api/v1/namespaces/prod"))["status"]["phase"] == "Active"

    # Minikube restarts (old token invalid), make k8s-reader-kubeconfig writes a new one.
    cluster.valid = "token-after-restart"
    write_atomically(path, "token-after-restart")
    await client.get_json("/api/v1/namespaces/prod")
    assert cluster.tokens == ["token-before-restart", "token-after-restart"]  # no 401 at all
    await client.aclose()


async def test_401_reloads_the_file_once_even_if_it_looks_unchanged(tmp_path: Path) -> None:
    """Same size, inode and mtime (e.g. rewritten in place within one tick): the 401 path."""
    path = tmp_path / "aiops-reader.kubeconfig"
    path.write_text(KUBECONFIG.format(token="token-aaaa"))
    before = path.stat()
    cluster = Cluster(valid="token-aaaa")
    client = client_for(path, cluster)
    await client.get_json("/api/v1/namespaces/prod")

    cluster.valid = "token-bbbb"
    path.write_text(KUBECONFIG.format(token="token-bbbb"))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    await client.get_json("/api/v1/namespaces/prod")
    assert cluster.tokens == ["token-aaaa", "token-aaaa", "token-bbbb"]  # one 401, one retry
    await client.aclose()


async def test_still_invalid_after_reload_says_how_to_fix_it(tmp_path: Path) -> None:
    path = tmp_path / "aiops-reader.kubeconfig"
    write_atomically(path, "expired")
    cluster = Cluster(valid="something-else")
    client = client_for(path, cluster)
    with pytest.raises(KubeError, match="make k8s-reader-kubeconfig"):
        await client.get_json("/api/v1/namespaces/prod")
    assert cluster.tokens == ["expired", "expired"]  # retried exactly once
    await client.aclose()


async def test_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    client = client_for(tmp_path / "nope.kubeconfig", Cluster(valid="x"))
    with pytest.raises(KubeError, match="credentials unavailable"):
        await client.get_json("/api/v1/namespaces/prod")
