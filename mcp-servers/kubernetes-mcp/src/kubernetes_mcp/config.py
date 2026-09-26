"""Server settings (env) and Kubernetes credentials (kubeconfig or in-cluster).

Only bearer-token credentials are accepted: the server is meant to run as a read-only
ServiceAccount (deploy/k8s/rbac). A kubeconfig with client certificates (typically an
admin identity) or an exec/auth-provider plugin is refused, so the server can't be
started with more privileges than intended by accident.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

IN_CLUSTER_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class ConfigError(Exception):
    pass


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


@dataclass(frozen=True)
class ServerSettings:
    kubeconfig: str | None = None  # path; None = in-cluster ServiceAccount
    context: str | None = None  # kubeconfig context (default: current-context)
    api_server: str | None = None  # override the kubeconfig server URL
    allowed_namespaces: tuple[str, ...] = ("prod",)
    max_results: int = 200  # items per list call
    max_scan: int = 2000  # events / ReplicaSets scanned per call (paged)
    max_log_lines: int = 300  # get_pod_logs tail cap
    max_log_bytes: int = 64_000  # get_pod_logs size cap
    max_since_hours: float = 48.0  # events / logs look-back cap
    query_timeout_s: float = 15.0

    @classmethod
    def from_env(cls) -> ServerSettings:
        return cls(
            kubeconfig=os.environ.get("KUBECONFIG") or None,
            context=os.environ.get("K8S_CONTEXT") or None,
            api_server=os.environ.get("K8S_API_SERVER") or None,
            allowed_namespaces=_csv(_env("ALLOWED_NAMESPACES", "prod")),
            max_results=int(_env("MAX_RESULTS", "200")),
            max_scan=int(_env("MAX_SCAN", "2000")),
            max_log_lines=int(_env("MAX_LOG_LINES", "300")),
            max_log_bytes=int(_env("MAX_LOG_BYTES", "64000")),
            max_since_hours=float(_env("MAX_SINCE_HOURS", "48")),
            query_timeout_s=float(_env("QUERY_TIMEOUT_S", "15")),
        )


@dataclass(frozen=True)
class KubeCredentials:
    server: str
    token: str = field(repr=False)
    ca_pem: str | None = None  # None = system trust store
    insecure: bool = False


def _pick(items: list[dict[str, Any]], name: str, what: str) -> dict[str, Any]:
    for item in items or []:
        if item.get("name") == name:
            value: dict[str, Any] = item.get(what) or {}
            return value
    raise ConfigError(f"kubeconfig has no {what} named '{name}'")


def _read(path: str, base: Path) -> str:
    file = Path(path)
    return (file if file.is_absolute() else base / file).read_text()


def credentials_from_kubeconfig(
    path: str, context: str | None = None, api_server: str | None = None
) -> KubeCredentials:
    file = Path(path)
    try:
        config: dict[str, Any] = yaml.safe_load(file.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read kubeconfig {path}: {exc}") from exc
    name = context or config.get("current-context")
    if not name:
        raise ConfigError("kubeconfig has no current-context; set K8S_CONTEXT")
    ctx = _pick(config.get("contexts", []), name, "context")
    cluster = _pick(config.get("clusters", []), str(ctx.get("cluster")), "cluster")
    user = _pick(config.get("users", []), str(ctx.get("user")), "user")

    if any(k in user for k in ("client-certificate", "client-certificate-data", "exec")) or (
        "auth-provider" in user
    ):
        raise ConfigError(
            "only bearer-token credentials are supported (use the read-only ServiceAccount "
            "kubeconfig from scripts/k8s-reader-kubeconfig.sh), not client certificates or exec plugins"
        )
    token = user.get("token")
    if not token and user.get("tokenFile"):
        token = _read(str(user["tokenFile"]), file.parent).strip()
    if not token:
        raise ConfigError(f"kubeconfig user '{ctx.get('user')}' has no token")

    ca_pem: str | None = None
    if cluster.get("certificate-authority-data"):
        ca_pem = base64.b64decode(cluster["certificate-authority-data"]).decode()
    elif cluster.get("certificate-authority"):
        ca_pem = _read(str(cluster["certificate-authority"]), file.parent)
    server = api_server or cluster.get("server")
    if not server:
        raise ConfigError("kubeconfig cluster has no server URL")
    return KubeCredentials(
        server=str(server).rstrip("/"),
        token=str(token),
        ca_pem=ca_pem,
        insecure=bool(cluster.get("insecure-skip-tls-verify", False)),
    )


def credentials_in_cluster(api_server: str | None = None) -> KubeCredentials:
    host, port = (
        os.environ.get("KUBERNETES_SERVICE_HOST"),
        os.environ.get("KUBERNETES_SERVICE_PORT", "443"),
    )
    token_file = IN_CLUSTER_DIR / "token"
    if not (api_server or host) or not token_file.is_file():
        raise ConfigError(
            "no KUBECONFIG set and not running in a cluster; mount the read-only kubeconfig "
            "(make k8s-reader-kubeconfig) and set KUBECONFIG"
        )
    ca = IN_CLUSTER_DIR / "ca.crt"
    return KubeCredentials(
        server=(api_server or f"https://{host}:{port}").rstrip("/"),
        token=token_file.read_text().strip(),
        ca_pem=ca.read_text() if ca.is_file() else None,
    )


def load_credentials(settings: ServerSettings) -> KubeCredentials:
    if settings.kubeconfig:
        return credentials_from_kubeconfig(
            settings.kubeconfig, settings.context, settings.api_server
        )
    return credentials_in_cluster(settings.api_server)
