from __future__ import annotations

import base64
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kubernetes_mcp.config import (
    ConfigError,
    ServerSettings,
    credentials_from_kubeconfig,
    load_credentials,
)
from kubernetes_mcp.guards import (
    GuardError,
    check_name,
    check_namespace,
    check_selector,
    clamp_limit,
    log_since_seconds,
    parse_since,
)

CA = "-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n"
NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)


def kubeconfig(tmp_path: Path, user: str) -> Path:
    path = tmp_path / "reader.kubeconfig"
    ca = base64.b64encode(CA.encode()).decode()
    path.write_text(
        f"""apiVersion: v1
kind: Config
clusters:
  - name: aiops
    cluster: {{server: "https://172.21.0.100:8443/", certificate-authority-data: {ca}}}
users:
  - name: reader
    user: {user}
contexts:
  - name: reader@aiops
    context: {{cluster: aiops, user: reader}}
current-context: reader@aiops
"""
    )
    return path


def test_token_kubeconfig(tmp_path: Path) -> None:
    creds = credentials_from_kubeconfig(str(kubeconfig(tmp_path, "{token: abc.def}")))
    assert creds.server == "https://172.21.0.100:8443"
    assert creds.token == "abc.def" and creds.ca_pem == CA
    assert "abc.def" not in repr(creds)  # never logged


def test_token_file_and_server_override(tmp_path: Path) -> None:
    (tmp_path / "token").write_text("from-file\n")
    path = kubeconfig(tmp_path, "{tokenFile: token}")
    creds = load_credentials(
        ServerSettings(kubeconfig=str(path), api_server="https://127.0.0.1:50079")
    )
    assert creds.token == "from-file" and creds.server == "https://127.0.0.1:50079"


@pytest.mark.parametrize(
    "user",
    [
        "{client-certificate-data: eA==, client-key-data: eA==}",
        "{exec: {command: aws}}",
        "{auth-provider: {name: gcp}}",
    ],
)
def test_admin_style_credentials_are_refused(tmp_path: Path, user: str) -> None:
    with pytest.raises(ConfigError, match="only bearer-token credentials"):
        credentials_from_kubeconfig(str(kubeconfig(tmp_path, user)))


def test_missing_context_and_token(tmp_path: Path) -> None:
    path = kubeconfig(tmp_path, "{username: x}")
    with pytest.raises(ConfigError, match="has no token"):
        credentials_from_kubeconfig(str(path))
    with pytest.raises(ConfigError, match="no context named 'other'"):
        credentials_from_kubeconfig(str(path), context="other")
    with pytest.raises(ConfigError, match="cannot read kubeconfig"):
        credentials_from_kubeconfig(str(tmp_path / "missing"))


def test_not_in_cluster_without_kubeconfig() -> None:
    with pytest.raises(ConfigError, match="not running in a cluster"):
        load_credentials(ServerSettings(kubeconfig=None))


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOWED_NAMESPACES", "prod, staging ,")
    monkeypatch.setenv("MAX_LOG_LINES", "50")
    settings = ServerSettings.from_env()
    assert settings.allowed_namespaces == ("prod", "staging")
    assert settings.max_log_lines == 50 and settings.max_scan == 2000


def test_name_namespace_selector_guards() -> None:
    assert check_namespace(" prod ", ("prod",)) == "prod"
    with pytest.raises(GuardError, match="not allowed"):
        check_namespace("kube-system", ("prod",))
    assert check_name("payment-service-847d4fc6f9-dtmf8") == "payment-service-847d4fc6f9-dtmf8"
    for bad in ("Payment", "a/b", "-x", "", "x" * 300):
        with pytest.raises(GuardError):
            check_name(bad)
    assert (
        check_selector("app in (redis, postgres),tier!=db") == "app in (redis, postgres),tier!=db"
    )
    assert check_selector("  ") is None
    with pytest.raises(GuardError, match="more than 10"):
        check_selector(",".join(f"k{i}=v" for i in range(11)))
    with pytest.raises(GuardError, match="invalid label_selector"):
        check_selector("app=`id`")
    assert clamp_limit(500, 200) == 200


def test_since_parsing() -> None:
    assert parse_since(None, 48, NOW) is None
    assert parse_since("30m", 48, NOW) == datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    assert parse_since("2026-09-25T10:00:00", 48, NOW) == datetime(2026, 9, 25, 10, 0, tzinfo=UTC)
    assert parse_since("2026-09-25T15:30:00+05:30", 48, NOW) == datetime(
        2026, 9, 25, 10, 0, tzinfo=UTC
    )
    with pytest.raises(GuardError, match="more than 48 hours"):
        parse_since("3d", 48, NOW)
    assert log_since_seconds("1h", 48, NOW) == 3600
    assert log_since_seconds(None, 2, NOW) == 7200
