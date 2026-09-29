"""Settings -> Integrations (PR-046): the overlay on the profile, validation, write-only
encrypted secrets, the audit trail, auth, test connection and the no-restart reload.

SQLite store, in-process fake MCP servers, zero network and zero tokens. Secrets are built
at runtime (the repository is public; gitleaks runs in CI)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from cryptography.fernet import Fernet
from fastapi import Request
from pydantic import SecretStr

from aiops.api import models as m
from aiops.api.context import ApiContext, default_executor_factory
from aiops.api.errors import ApiError
from aiops.api.integration_service import IntegrationService
from aiops.api.integrations import require_writer
from aiops.core.config import ApiConfig, MCPServerConfig, Settings, load_settings
from aiops.core.integrations import (
    CAPABILITIES,
    IntegrationError,
    IntegrationOverride,
    IntegrationUpdate,
    editable_defaults,
    effective_settings,
    is_secret_key,
    mask_url,
    plan_update,
)
from aiops.core.secrets import SECRETS_KEY_VAR, SecretBox, SecretsKeyError, secret_hint
from aiops.mcp.client import MCPClient
from aiops.store import tables
from aiops.store.integrations import IntegrationStore
from aiops.store.repository import InvestigationStore
from tests.api_support import api_client, make_context
from tests.conftest import REPO_ROOT
from tests.unit.test_doctor import logs_server

CONFIG = REPO_ROOT / "config"
#: Built at runtime: never a real credential.
SECRET = "Bearer " + "-".join(["fake", "mcp", "token", uuid4().hex])
API_KEY = uuid4().hex
ALICE_KEY = uuid4().hex


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", CONFIG)


@pytest.fixture
def box() -> SecretBox:
    return SecretBox(Fernet.generate_key())


@pytest.fixture
def store(tmp_path: Path) -> InvestigationStore:
    store = InvestigationStore(f"sqlite:///{tmp_path / 'api.db'}")
    store.migrate()
    return store


def context(
    settings: Settings,
    store: InvestigationStore,
    box: SecretBox | None,
    **mcp: Any,
) -> ApiContext:
    ctx = make_context(settings, store)
    ctx.integrations = IntegrationService(
        settings, IntegrationStore(store.engine), box=box, mcp_overrides=mcp
    )
    return ctx


def dumps(value: Any) -> str:
    return json.dumps(value, default=str)


# --------------------------------------------------------------------------- overlay merge


def test_overlay_merges_over_the_profile_and_reverts(settings: Settings) -> None:
    update = IntegrationUpdate(
        fields={
            "mcp.url": "http://logs-mcp.acme.internal:8101/mcp",
            "settings.fields.level": "log.level",
            "settings.error_levels": ["error", "fatal"],
            "limits.max_results": 250,
        }
    )
    override, changes = plan_update(settings, None, "logs", update, None)
    assert {c.field for c in changes} == set(update.fields)
    assert all(c.change == "set" for c in changes)

    merged, notes = effective_settings(settings, {"logs": override}, None)
    logs = merged.capabilities["logs"]
    assert notes == {}
    assert logs.mcp.url == "http://logs-mcp.acme.internal:8101/mcp"
    assert logs.settings["fields"]["level"] == "log.level"
    assert logs.settings["fields"]["message"] == "message"  # untouched keys keep the YAML
    assert logs.settings["error_levels"] == ["error", "fatal"]
    assert logs.limits.max_results == 250
    assert logs.tool_allowlist == settings.capabilities["logs"].tool_allowlist
    # the profile itself is not modified, and other capabilities are the same objects
    assert settings.capabilities["logs"].settings["fields"]["level"] == "level"
    assert merged.capabilities["metrics"] is settings.capabilities["metrics"]

    reverted, changes = plan_update(
        settings,
        override,
        "logs",
        IntegrationUpdate(fields={"settings.fields.level": None, "limits.max_results": 1000}),
        None,
    )
    assert {(c.field, c.change) for c in changes} == {
        ("settings.fields.level", "reverted"),
        ("limits.max_results", "reverted"),  # back to the profile value = not an override
    }
    assert set(reverted.fields) == {"mcp.url", "settings.error_levels"}
    reset, changes = plan_update(settings, reverted, "logs", IntegrationUpdate(reset=True), None)
    assert reset.empty
    assert {c.field for c in changes} == {"mcp.url", "settings.error_levels"}


def test_declared_but_empty_settings_are_editable(settings: Settings) -> None:
    """``ui_link_template: ${K8S_UI_LINK_TEMPLATE:-}`` resolves empty, yet it's settable."""
    defaults = editable_defaults(settings, "k8s")
    assert defaults["settings.ui_link_template"] is None
    assert "mcp.url" in defaults and "limits.max_results" in defaults
    link = "https://console.acme.example/{namespace}/{kind}/{name}"
    override, _ = plan_update(
        settings,
        None,
        "k8s",
        IntegrationUpdate(fields={"settings.ui_link_template": link}),
        None,
    )
    merged, _ = effective_settings(settings, {"k8s": override}, None)
    assert merged.capabilities["k8s"].settings["ui_link_template"] == link


def test_enabled_and_provider(settings: Settings) -> None:
    override, changes = plan_update(
        settings, None, "tickets", IntegrationUpdate(enabled=False, provider="jira"), None
    )
    assert {c.field for c in changes} == {"enabled", "provider"}
    merged, _ = effective_settings(settings, {"tickets": override}, None)
    assert merged.capabilities["tickets"].enabled is False
    assert merged.capabilities["tickets"].provider == "jira"


def test_an_override_that_no_longer_validates_is_skipped_with_a_note(
    settings: Settings,
) -> None:
    broken = IntegrationOverride(settings.profile, "logs", fields={"mcp.timeout_s": -5})
    merged, notes = effective_settings(settings, {"logs": broken}, None)
    assert merged.capabilities["logs"] is settings.capabilities["logs"]
    assert "override ignored" in notes["logs"][0]


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    ("capability", "update", "message"),
    [
        ("logs", {"fields": {"settings.fields.level": 7}}, "must be text"),
        ("logs", {"fields": {"limits.max_results": "many"}}, "must be a number"),
        ("logs", {"fields": {"limits.max_results": 2.5}}, "whole number"),
        ("logs", {"fields": {"settings.error_levels": "ERROR"}}, "must be a list"),
        ("k8s", {"fields": {"settings.include_dependencies": "yes"}}, "true or false"),
        ("logs", {"fields": {"tool_allowlist": ["delete_index"]}}, "unknown field"),
        ("logs", {"fields": {"settings.nope": "x"}}, "unknown field"),
        ("logs", {"fields": {"mcp.url": "ftp://logs"}}, r"http\(s\) URL"),
        ("logs", {"fields": {"mcp.url": "http://user:pw@logs:8101/mcp"}}, "credentials"),
        ("logs", {"fields": {"settings.fields.level": "a\nb"}}, "control characters"),
        ("logs", {"provider": "splunk"}, "not an implemented logs provider"),
        ("logs", {"secrets": {"Bad Header": "x"}}, "HTTP header name"),
        ("tickets", {"fields": {"settings.write_requires_approval": False}}, "guardrail"),
    ],
)
def test_invalid_updates_are_refused(
    settings: Settings, box: SecretBox, capability: str, update: dict[str, Any], message: str
) -> None:
    with pytest.raises(IntegrationError, match=message):
        plan_update(settings, None, capability, IntegrationUpdate(**update), box)


def test_unconfigured_capability_is_refused(settings: Settings) -> None:
    without = settings.model_copy(
        update={"capabilities": {k: v for k, v in settings.capabilities.items() if k != "code"}}
    )
    with pytest.raises(IntegrationError) as err:
        plan_update(without, None, "code", IntegrationUpdate(fields={"mcp.timeout_s": 5}), None)
    assert err.value.status == 409


def test_secret_looking_settings_are_not_plain_fields() -> None:
    assert is_secret_key("settings.api_key")
    assert is_secret_key("settings.auth.token")
    assert is_secret_key("settings.client_secret")
    assert not is_secret_key("settings.project_key")  # a Jira project key is not a secret
    assert not is_secret_key("settings.symptom_terms")
    assert mask_url("https://u:p@mcp.acme.example/mcp") == "https://***@mcp.acme.example/mcp"


# --------------------------------------------------------------------------- secrets


def test_secret_box_round_trip_and_key_errors(
    box: SecretBox, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = box.encrypt(SECRET)
    assert SECRET not in token
    assert box.decrypt(token) == SECRET
    with pytest.raises(SecretsKeyError, match="rotated"):
        SecretBox(Fernet.generate_key()).decrypt(token)
    with pytest.raises(SecretsKeyError, match="not a valid Fernet key"):
        SecretBox("too-short")
    monkeypatch.delenv(SECRETS_KEY_VAR, raising=False)
    assert SecretBox.from_env() is None
    monkeypatch.setenv(SECRETS_KEY_VAR, Fernet.generate_key().decode())
    assert SecretBox.from_env() is not None
    assert secret_hint(SECRET) == SECRET[-4:]
    assert secret_hint("short") is None


def test_secrets_are_refused_without_a_key(settings: Settings) -> None:
    with pytest.raises(IntegrationError, match=SECRETS_KEY_VAR) as err:
        plan_update(
            settings, None, "logs", IntegrationUpdate(secrets={"Authorization": SECRET}), None
        )
    assert err.value.code == "secrets_key_missing"


def test_secret_becomes_an_mcp_header(settings: Settings, box: SecretBox) -> None:
    override, changes = plan_update(
        settings, None, "logs", IntegrationUpdate(secrets={"Authorization": SECRET}), box
    )
    assert [(c.field, c.change) for c in changes] == [("secrets.Authorization", "set")]
    stored = override.secrets["Authorization"]
    assert SECRET not in stored.ciphertext and stored.hint == SECRET[-4:]
    merged, _ = effective_settings(settings, {"logs": override}, box)
    mcp = merged.capabilities["logs"].mcp
    assert mcp.header_values() == {"Authorization": SECRET}
    assert SECRET not in dumps(merged.safe_dump())  # SecretStr masks every dump
    # without the key (or with another one) the header is left out, with a note
    for other in (None, SecretBox(Fernet.generate_key())):
        merged, notes = effective_settings(settings, {"logs": override}, other)
        assert merged.capabilities["logs"].mcp.headers == {}
        assert notes["logs"]
    # the MCP client sends them: a fresh header-carrying transport per connect attempt
    client = MCPClient.from_config("logs", mcp)
    assert client._server() is not client._server()
    assert MCPClient.from_config("logs", settings.capabilities["logs"].mcp)._server() == (
        settings.capabilities["logs"].mcp.url
    )


def test_header_config_validation() -> None:
    with pytest.raises(ValueError, match="single line"):
        MCPServerConfig(transport="http", url="http://x/mcp", headers={"X-A": SecretStr("a\nb")})
    with pytest.raises(ValueError, match="http MCP servers only"):
        MCPServerConfig(transport="stdio", command="x", headers={"X-A": SecretStr("a")})


# --------------------------------------------------------------------------- API


async def test_api_lists_every_capability_without_secret_values(
    settings: Settings, store: InvestigationStore, box: SecretBox
) -> None:
    ctx = context(settings, store, box)
    async with api_client(ctx) as client:
        listed = await client.get("/api/integrations")
        saved = await client.put(
            "/api/integrations/logs",
            json={
                "fields": {"settings.fields.level": "log.level"},
                "secrets": {"Authorization": SECRET, "X-Scope-OrgID": "tenant-a"},
            },
        )
        one = await client.get("/api/integrations/logs")
        again = await client.get("/api/integrations")
        audit = await client.get("/api/integrations/audit")
        missing = await client.get("/api/integrations/nope")
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["secrets_enabled"] is True
    caps = [i["capability"] for i in body["items"]]
    assert caps[:7] == ["logs", "metrics", "alerts", "k8s", "code", "tickets", "knowledge"]
    logs = body["items"][0]
    assert logs["provider"] == "elasticsearch" and logs["status"] == "ok"
    assert "loki" in logs["providers"]
    fields = {f["key"]: f for f in logs["fields"]}
    assert fields["settings.fields.level"]["value"] == "level"
    assert fields["mcp.url"]["editable"] is True

    assert saved.status_code == 200, saved.text
    out = saved.json()
    assert {c["field"] for c in out["changes"]} == {
        "settings.fields.level",
        "secrets.Authorization",
        "secrets.X-Scope-OrgID",
    }
    secrets = {s["name"]: s for s in out["integration"]["secrets"]}
    assert secrets["Authorization"] == {
        "name": "Authorization",
        "configured": True,
        "last4": SECRET[-4:],
        "source": "ui",
        "usable": True,
    }
    assert secrets["X-Scope-OrgID"]["last4"] is None  # short values show nothing
    for response in (saved, one, again, audit):
        assert SECRET not in response.text
        assert SECRET[:-4] not in response.text
        assert "tenant-a" not in response.text
    field = {f["key"]: f for f in one.json()["fields"]}["settings.fields.level"]
    assert (field["value"], field["default"], field["overridden"]) == ("log.level", "level", True)
    assert missing.status_code == 404


async def test_saving_secrets_without_a_key_is_refused_with_a_clear_message(
    settings: Settings, store: InvestigationStore
) -> None:
    ctx = context(settings, store, None)
    async with api_client(ctx) as client:
        listed = await client.get("/api/integrations")
        refused = await client.put(
            "/api/integrations/logs", json={"secrets": {"Authorization": SECRET}}
        )
    assert listed.json()["secrets_enabled"] is False
    assert refused.status_code == 422
    error = refused.json()["error"]
    assert error["code"] == "secrets_key_missing"
    assert SECRETS_KEY_VAR in error["message"]
    assert SECRET not in refused.text


async def test_secrets_are_encrypted_at_rest(
    settings: Settings, store: InvestigationStore, box: SecretBox
) -> None:
    ctx = context(settings, store, box)
    async with api_client(ctx) as client:
        saved = await client.put(
            "/api/integrations/metrics", json={"secrets": {"Authorization": SECRET}}
        )
        assert saved.status_code == 200, saved.text
        async with store.engine.connect() as conn:
            rows = (await conn.execute(sa.select(tables.integration_override))).all()
            audit = (await conn.execute(sa.select(tables.integration_audit))).all()
    assert len(rows) == 1
    raw = dumps([dict(r._mapping) for r in rows])
    assert SECRET not in raw and SECRET[:-4] not in raw
    token = rows[0].secrets["Authorization"]["ciphertext"]
    assert box.decrypt(token) == SECRET
    assert SECRET not in dumps([dict(r._mapping) for r in audit])


async def test_validation_errors_are_422_and_nothing_is_stored(
    settings: Settings, store: InvestigationStore, box: SecretBox
) -> None:
    ctx = context(settings, store, box)
    async with api_client(ctx) as client:
        bad_type = await client.put(
            "/api/integrations/logs", json={"fields": {"limits.max_results": "lots"}}
        )
        bad_model = await client.put(
            "/api/integrations/logs", json={"fields": {"mcp.timeout_s": -1}}
        )
        # tickets/jira needs settings.project_key: blanking it fails `profile validate`
        semantic = await client.put(
            "/api/integrations/tickets",
            json={"provider": "jira", "fields": {"settings.project_key": ""}},
        )
        audit = await client.get("/api/integrations/audit")
    assert bad_type.status_code == 422 and "must be a number" in bad_type.text
    assert bad_model.status_code == 422 and "timeout_s" in bad_model.text
    assert semantic.status_code == 422 and "project_key" in semantic.text
    assert audit.json() == []
    assert ctx.integrations is not None and ctx.integrations.overrides == {}


async def test_changes_are_audited_by_field_never_by_value(
    settings: Settings,
    store: InvestigationStore,
    box: SecretBox,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = context(settings, store, box)
    caplog.set_level(logging.DEBUG)
    async with api_client(ctx) as client:
        await client.put(
            "/api/integrations/alerts",
            json={
                "fields": {"settings.labels.severity": "priority"},
                "secrets": {"Authorization": SECRET},
            },
        )
        await client.put("/api/integrations/alerts", json={"secrets": {"Authorization": None}})
        noop = await client.put("/api/integrations/alerts", json={})
        entries = (await client.get("/api/integrations/audit?capability=alerts")).json()
    assert noop.json()["changes"] == []
    assert [e["action"] for e in entries] == ["update", "update"]  # the no-op isn't audited
    latest, first = entries
    assert first["actor"] == "anonymous"  # auth is off in this test
    assert {(c["field"], c["change"]) for c in first["changes"]} == {
        ("settings.labels.severity", "set"),
        ("secrets.Authorization", "set"),
    }
    assert latest["changes"] == [{"field": "secrets.Authorization", "change": "cleared"}]
    assert "priority" not in dumps(entries)  # field names only, never values
    assert SECRET not in caplog.text and SECRET[:-4] not in caplog.text


async def test_save_and_test_need_an_authenticated_caller_when_auth_is_on(
    settings: Settings, store: InvestigationStore, box: SecretBox
) -> None:
    keyed = settings.model_copy(
        update={
            "api": ApiConfig(api_key=SecretStr(API_KEY), api_keys={"alice": SecretStr(ALICE_KEY)})
        }
    )
    ctx = context(keyed, store, box)
    body = {"fields": {"settings.fields.level": "log.level"}}
    async with api_client(ctx) as client:
        anonymous_read = await client.get("/api/integrations")
        anonymous_save = await client.put("/api/integrations/logs", json=body)
        anonymous_test = await client.post("/api/integrations/logs/test")
        wrong = await client.put(
            "/api/integrations/logs", json=body, headers={"X-API-Key": uuid4().hex}
        )
        shared = await client.put(
            "/api/integrations/logs", json=body, headers={"X-API-Key": API_KEY}
        )
        named = await client.put(
            "/api/integrations/logs",
            json={"fields": {"settings.fields.level": None}},
            headers={"X-API-Key": ALICE_KEY},
        )
        read = await client.get("/api/integrations/audit", headers={"X-API-Key": API_KEY})
        security = await client.get("/api/integrations", headers={"X-API-Key": API_KEY})
    assert anonymous_read.status_code == 401  # auth on: every /api route needs the key
    assert anonymous_save.status_code == 401
    assert anonymous_test.status_code == 401
    assert wrong.status_code == 401
    assert shared.status_code == 200, shared.text
    assert named.status_code == 200, named.text
    assert [e["actor"] for e in read.json()] == ["alice", "api-key"]
    # the API's security headers stay on the new routes
    assert security.headers["content-security-policy"].startswith("default-src 'none'")
    assert security.headers["x-frame-options"] == "DENY"


def test_require_writer_refuses_anonymous_when_auth_is_on(
    settings: Settings, store: InvestigationStore
) -> None:
    keyed = settings.model_copy(update={"api": ApiConfig(api_key=SecretStr(API_KEY))})
    ctx = make_context(keyed, store)

    class _App:
        state = type("S", (), {"ctx": ctx})()

    request = Request({"type": "http", "app": _App(), "state": {}, "headers": []})
    with pytest.raises(ApiError) as err:
        require_writer(request)
    assert err.value.status == 401


async def test_saving_applies_to_new_investigations_without_a_restart(
    settings: Settings, store: InvestigationStore, box: SecretBox
) -> None:
    ctx = context(settings, store, box)
    url = "http://metrics-mcp.acme.internal:8103/mcp"
    async with api_client(ctx) as client:
        before = ctx.settings
        saved = await client.put("/api/integrations/metrics", json={"fields": {"mcp.url": url}})
        assert saved.status_code == 200, saved.text
        # everything that starts from now on uses the new settings ...
        assert ctx.settings.capabilities["metrics"].mcp.url == url
        assert ctx.runner.settings is ctx.settings
        assert ctx.health.settings is ctx.settings
        executor = default_executor_factory(lambda: ctx.settings)(ctx.approvals)
        assert executor.mcp.settings is ctx.settings
        # ... while the API's own config is untouched and the YAML base is unchanged
        assert ctx.settings.api == before.api
        assert before.capabilities["metrics"].mcp.url != url
        assert ctx.integrations is not None
        assert ctx.integrations.base.capabilities["metrics"].mcp.url != url

    # a restarted API loads the overlay from the store
    restarted = context(settings, store, box)
    async with api_client(restarted):
        assert restarted.settings.capabilities["metrics"].mcp.url == url


async def test_test_connection_runs_the_doctor_checks_for_one_capability(
    settings: Settings, store: InvestigationStore, box: SecretBox
) -> None:
    ctx = context(settings, store, box, logs=logs_server())
    async with api_client(ctx) as client:
        saved = await client.post("/api/integrations/logs/test")
        draft_down = await client.post(
            "/api/integrations/metrics/test",
            json={"fields": {"mcp.url": "http://127.0.0.1:9/mcp", "mcp.timeout_s": 1}},
        )
        disabled = await client.post("/api/integrations/code/test", json={"enabled": False})
        invalid = await client.post(
            "/api/integrations/logs/test", json={"fields": {"mcp.url": "nope"}}
        )
        audit = await client.get("/api/integrations/audit")
    assert saved.status_code == 200, saved.text
    result = saved.json()
    checks = {c["check"]: c for c in result["checks"]}
    assert {"config", "reachability", "contract", "smoke"} <= set(checks)
    assert checks["reachability"]["status"] == "ok"
    assert checks["reachability"]["detail"].startswith("in-process: 2 tools")
    # the fake server lacks two allowlisted tools: the doctor's contract check says which
    assert checks["contract"]["status"] == "fail"
    assert "execute_esql" in checks["contract"]["detail"]
    assert result["status"] == "fail"
    assert all(c["check"] != "llm" for c in result["checks"])  # one capability only

    down = draft_down.json()
    assert down["status"] == "fail"
    reach = next(c for c in down["checks"] if c["check"] == "reachability")
    assert reach["status"] == "fail" and "127.0.0.1:9" in reach["detail"]

    assert disabled.json()["status"] == "skip"
    assert invalid.status_code == 422
    assert audit.json() == []  # testing never stores anything
    assert ctx.settings.capabilities["metrics"].mcp.url == settings.capabilities["metrics"].mcp.url


def test_the_web_ui_demo_dataset_matches_the_api_model(settings: Settings) -> None:
    """frontend/src/demo/integrations.json (demo mode) follows the same contract."""
    path = REPO_ROOT / "frontend" / "src" / "demo" / "integrations.json"
    demo = m.IntegrationList.model_validate_json(path.read_text())
    assert [i.capability for i in demo.items] == list(CAPABILITIES)
    for item in demo.items:
        assert {f.key for f in item.fields} == set(editable_defaults(settings, item.capability))
        assert all(s.last4 is None or len(s.last4) <= 4 for s in item.secrets)
