"""Redaction coverage (PR-042): every secret family, JSON secret keys, PII toggles, no
false positives on operational data, linear time on large payloads."""

from __future__ import annotations

import json
import time

import pytest
from pydantic import ValidationError

from aiops.core.config import GuardrailsConfig
from aiops.core.guardrails.redaction import (
    PII_KINDS,
    SECRET_KINDS,
    SUPPORTED_KINDS,
    Redactor,
    is_secret_key,
)
from tests.security import payloads as p

ALL = sorted(SUPPORTED_KINDS)


@pytest.mark.parametrize(
    ("raw", "secret", "kind"),
    [
        (lambda: f"key {p.fake_private_key()} end", "MIIEvQIBADAN", "private_keys"),
        (
            lambda: '{"private_key": "' + p.fake_private_key().replace("\n", "\\n") + '"}',
            "MIIEvQIBADAN",
            "private_keys",
        ),
        (lambda: f"dsn={p.fake_connection_string()}", p.fake_db_password(), "connection_strings"),
        (
            lambda: "redis://:" + p.fake_db_password() + "@cache:6379/0",
            p.fake_db_password(),
            "connection_strings",
        ),
        (lambda: p.fake_azure_connection_string(), "abcd" * 10, "connection_strings"),
        (lambda: f"id {p.fake_aws_key_id()}", p.fake_aws_key_id(), "cloud_credentials"),
        (
            lambda: f"aws_secret_access_key = {p.fake_aws_secret()}",
            p.fake_aws_secret(),
            "cloud_credentials",
        ),
        (lambda: f"gcp {p.fake_gcp_key()}", p.fake_gcp_key(), "cloud_credentials"),
        (lambda: "oauth ya29." + "a0AfH6SM" * 4, "a0AfH6SM" * 4, "cloud_credentials"),
        (lambda: f"gh {p.fake_github_token()}", p.fake_github_token(), "saas_tokens"),
        (
            lambda: "pat github_pat_" + "11ABCDEFG0" * 6,
            "11ABCDEFG0" * 6,
            "saas_tokens",
        ),
        (lambda: f"gl {p.fake_gitlab_token()}", p.fake_gitlab_token(), "saas_tokens"),
        (lambda: f"slack {p.fake_slack_token()}", p.fake_slack_token(), "saas_tokens"),
        (lambda: f"hook {p.fake_slack_webhook()}", p.fake_slack_webhook(), "saas_tokens"),
        (lambda: f"jira {p.fake_atlassian_token()}", p.fake_atlassian_token(), "saas_tokens"),
        (lambda: f"jwt {p.fake_jwt()}", p.fake_jwt(), "jwt"),
        (
            lambda: "Authorization: Bearer abcdef1234567890xyz",
            "abcdef1234567890xyz",
            "bearer_tokens",
        ),
        (
            lambda: "Authorization: Basic " + "YWxhZGRpbjpvcGVuc2VzYW1l",
            "YWxhZGRpbjpvcGVuc2VzYW1l",
            "bearer_tokens",
        ),
        (lambda: "db_password=" + p.fake_db_password(), p.fake_db_password(), "api_keys"),
        (lambda: '{"client_secret": "' + "cs-" + "9f8e7d6c5b" + '"}', "9f8e7d6c5b", "api_keys"),
        (lambda: "X-Api-Key: " + "k3y" + "0123456789", "k3y0123456789", "api_keys"),
        (lambda: "card " + p.fake_card(), p.fake_card(), "credit_cards"),
        (lambda: "amex 3782 822463 " + "10005", "3782 822463 10005", "credit_cards"),
        (lambda: "user bob@corp.example.com", "bob@corp.example.com", "emails"),
        (lambda: "peer 10.42.0.17:5432", "10.42.0.17", "ip_addresses"),
    ],
)
def test_each_secret_family_is_redacted(raw: object, secret: str, kind: str) -> None:
    text = raw()  # type: ignore[operator]
    assert secret in text
    redactor = Redactor(ALL)
    out = redactor.text(text)
    assert secret not in out, out
    assert f"[REDACTED:{kind}]" in out, out
    assert redactor.counts[kind] >= 1


def test_the_worst_case_line_is_fully_redacted() -> None:
    out = Redactor(ALL).text(p.leaky_log_line())
    leaked = [name for name, value in p.secrets().items() if value in out]
    assert not leaked


def test_secret_named_json_keys_are_redacted_whatever_the_value() -> None:
    data = {
        "password": "x",  # too short for any pattern
        "DB_PASSWORD": "hunter2",
        "Authorization": "Negotiate abc",
        "set-cookie": "session=abc; HttpOnly",
        "nested": [{"client_secret": "value"}],
        "tokens": 1234,  # a number (e.g. token usage) is never touched
        "token_count": 5,
        "passthrough": "ok",
    }
    out = Redactor(SECRET_KINDS).data(data)
    tag = "[REDACTED:api_keys]"
    assert out["password"] == out["DB_PASSWORD"] == tag
    assert out["Authorization"] == out["set-cookie"] == tag
    assert out["nested"][0]["client_secret"] == tag
    assert out["tokens"] == 1234 and out["token_count"] == 5 and out["passthrough"] == "ok"
    json.dumps(out)


@pytest.mark.parametrize(
    "benign",
    [
        "2026-09-25T10:08:00.000Z payment-service v1.8.2 started in 1234ms",
        "trace_id=96a931cfdc9709fe span_id=ab12cd34 request_id=0f8fad5b-d9cb-469f-a165-70867728950e",
        "commit 3f786850e387550fdab836ed7e6dc881de23001b by dev",
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        "http://payment-service:8080/api/v1/payments returned 503",
        "max_tokens=2048 tokens_used: 1523",
        "token expired for session; retrying",
        "order 1234567890123 total 99.95",
        "pool size 20/20 pending=37 timeout=5000ms",
    ],
)
def test_operational_data_is_not_redacted(benign: str) -> None:
    assert Redactor(SECRET_KINDS).text(benign) == benign


def test_pii_is_configurable() -> None:
    text = "alice@example.com from 10.0.0.1"
    assert Redactor(SECRET_KINDS).text(text) == text
    assert Redactor(PII_KINDS).text(text) == "[REDACTED:emails] from [REDACTED:ip_addresses]"
    assert Redactor(["emails"]).text(text) == "[REDACTED:emails] from 10.0.0.1"


def test_profile_groups_and_safe_default() -> None:
    assert GuardrailsConfig().redact == list(SECRET_KINDS)  # secrets on, even if omitted
    both = GuardrailsConfig(redact=["secrets", "pii"]).redact
    assert set(both) == SUPPORTED_KINDS and len(both) == len(SUPPORTED_KINDS)
    assert GuardrailsConfig(redact="secrets,emails").redact == [*SECRET_KINDS, "emails"]  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="unknown redaction kinds"):
        GuardrailsConfig(redact=["phone_numbers"])


def test_is_secret_key() -> None:
    assert all(
        is_secret_key(k)
        for k in ["password", "db_password", "X-Api-Key", "client_secret", "aws.secret", "Cookie"]
    )
    assert not any(is_secret_key(k) for k in ["tokens", "max_tokens", "passthrough", "keyspace"])


def test_large_payloads_are_linear() -> None:
    # ~3 MB of adversarial-looking but secret-free text: near-misses for every pattern.
    chunk = (
        "a-b-c-d-e-f_g.h password tokenizer key=v https://user@host/ -----BEGIN PUBLIC KEY----- "
        "4111 1111 1111 ghp_short AKIA eyJ.x.y 10.0.0 "
    )
    text = chunk * 25_000
    started = time.perf_counter()
    Redactor(ALL).text(text)
    assert time.perf_counter() - started < 10.0
