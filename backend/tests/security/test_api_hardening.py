"""API security boundary (PR-042): auth modes, approver identity, fault double-guard, rate
limits, body size, security headers, CORS and idempotency keys. In-process (httpx ASGI)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from aiops.api.security import RateLimiter
from aiops.core.config import ApiConfig, Settings, load_settings
from aiops.store.repository import InvestigationStore
from tests.api_support import api_client, make_context, wait_until_idle
from tests.unit.test_api import CONFIG

SHARED = uuid4().hex
ALICE = uuid4().hex
BOB = uuid4().hex


@pytest.fixture(scope="module")
def base() -> Settings:
    return load_settings("local", CONFIG)


@pytest.fixture
def store(tmp_path: Path) -> InvestigationStore:
    store = InvestigationStore(f"sqlite:///{tmp_path / 'api.db'}")
    store.migrate()
    return store


def with_api(settings: Settings, **api: Any) -> Settings:
    return settings.model_copy(update={"api": ApiConfig(**api)})


def keyed(settings: Settings, **api: Any) -> Settings:
    return with_api(
        settings,
        api_key=SecretStr(SHARED),
        api_keys={"alice": SecretStr(ALICE), "bob": SecretStr(BOB)},
        **api,
    )


async def _replayed(client: Any, ctx: Any, headers: dict[str, str]) -> str:
    created = await client.post(
        "/api/investigations", json={"question": "x", "scenario": "S1"}, headers=headers
    )
    assert created.status_code == 202, created.text
    await wait_until_idle(ctx)
    return str(created.json()["id"])


# --------------------------------------------------------------------------- auth


def test_auth_modes_and_oidc_is_reserved() -> None:
    assert ApiConfig().auth_mode == "none"
    assert ApiConfig(api_key=SecretStr(SHARED)).auth_mode == "api_key"
    parsed = ApiConfig(api_keys=f"alice:{ALICE}, bob:{BOB}")  # type: ignore[arg-type]
    assert parsed.auth_mode == "api_key" and set(parsed.api_keys) == {"alice", "bob"}
    with pytest.raises(ValidationError, match="16\\+ characters"):
        ApiConfig(api_keys="alice:short")  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="PR-045"):
        ApiConfig(auth="oidc", oidc={"issuer": "https://idp", "audience": "aiops"})  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match=r"needs api\.api_key"):
        ApiConfig(auth="api_key")


async def test_named_keys_authenticate_and_become_the_approver(
    base: Settings, store: InvestigationStore
) -> None:
    ctx = make_context(keyed(base), store)
    async with api_client(ctx) as client:
        assert (await client.get("/api/services")).status_code == 401
        bad = await client.get("/api/services", headers={"X-API-Key": ALICE[:-1] + "x"})
        assert bad.status_code == 401 and bad.headers["www-authenticate"]
        inv_id = await _replayed(client, ctx, {"X-API-Key": ALICE})
        drafts = [
            (
                await client.post(
                    f"/api/investigations/{inv_id}/tickets/draft",
                    json={"requested_by": "someone-else"},
                    headers={"X-API-Key": ALICE},
                )
            ).json()["approval_id"]
            for _ in range(2)
        ]
        shared = await client.post(
            f"/api/approvals/{drafts[0]}/approve",
            json={"by": "alice"},
            headers={"X-API-Key": SHARED},
        )
        spoofed = await client.post(
            f"/api/approvals/{drafts[0]}/approve",
            json={"by": "the-cto", "comment": "ok"},
            headers={"X-API-Key": BOB},
        )
        denied = await client.post(
            f"/api/approvals/{drafts[1]}/deny", json={"by": "x"}, headers={"X-API-Key": ALICE}
        )
    assert shared.status_code == 403
    assert shared.json()["error"]["code"] == "approver_identity_required"
    assert spoofed.status_code == 200, spoofed.text
    assert spoofed.json()["decided_by"] == "bob"  # the key's identity, not the body's claim
    assert spoofed.json()["requested_by"] == "alice"
    assert denied.json()["decided_by"] == "alice"


async def test_without_auth_the_demo_behaviour_is_unchanged(
    base: Settings, store: InvestigationStore
) -> None:
    ctx = make_context(base, store)
    async with api_client(ctx) as client:
        inv_id = await _replayed(client, ctx, {})
        draft = await client.post(f"/api/investigations/{inv_id}/tickets/draft")
        approved = await client.post(
            f"/api/approvals/{draft.json()['approval_id']}/approve", json={"by": "alice"}
        )
    assert approved.json()["decided_by"] == "alice"


# --------------------------------------------------------------------------- faults


async def test_fault_endpoints_need_the_flag_and_auth(
    base: Settings, store: InvestigationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIOPS_ENABLE_FAULTS", "1")
    open_ctx = make_context(base, store)
    async with api_client(open_ctx) as client:
        health = (await client.get("/api/health")).json()
        inject = await client.post("/api/scenarios/S1/inject")
        revert = await client.post("/api/scenarios/revert")
    assert health["faults_enabled"] is False
    assert inject.status_code == revert.status_code == 403
    assert inject.json()["error"]["code"] == "faults_need_auth"

    monkeypatch.delenv("AIOPS_ENABLE_FAULTS")
    locked = make_context(keyed(base), store)
    async with api_client(locked) as client:
        flag_off = await client.post("/api/scenarios/S1/inject", headers={"X-API-Key": SHARED})
    assert flag_off.status_code == 403 and flag_off.json()["error"]["code"] == "faults_disabled"


# --------------------------------------------------------------------------- rate limits


def test_token_bucket_refills() -> None:
    now = [0.0]
    limiter = RateLimiter({"investigations": "2/minute"}, clock=lambda: now[0])
    assert limiter.check("investigations", "a") is None
    assert limiter.check("investigations", "a") is None
    wait = limiter.check("investigations", "a")
    assert wait is not None and 29 < wait <= 30
    assert limiter.check("investigations", "b") is None  # per client
    assert limiter.check("other", "a") is None  # unlimited group
    now[0] = 30.0
    assert limiter.check("investigations", "a") is None


async def test_rate_limits_per_client_and_group(base: Settings, store: InvestigationStore) -> None:
    settings = keyed(base, rate_limits={"investigations": "2/minute", "approvals": "1/minute"})
    ctx = make_context(settings, store)
    body = {"question": "x", "scenario": "S9"}  # unknown: nothing starts, still counted
    async with api_client(ctx) as client:
        alice = [
            await client.post("/api/investigations", json=body, headers={"X-API-Key": ALICE})
            for _ in range(3)
        ]
        bob = await client.post("/api/investigations", json=body, headers={"X-API-Key": BOB})
        clarify = await client.post(
            "/api/investigations/inv-x/clarify", json={"answer": "a"}, headers={"X-API-Key": ALICE}
        )
        deny = [
            await client.post(
                "/api/approvals/act-x/deny", json={"by": "a"}, headers={"X-API-Key": BOB}
            )
            for _ in range(2)
        ]
    assert [r.status_code for r in alice] == [404, 404, 429]
    limited = alice[2]
    assert limited.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(limited.headers["retry-after"]) <= 30
    assert bob.status_code == 404 and clarify.status_code == 429
    assert [r.status_code for r in deny] == [404, 429]


# --------------------------------------------------------------------------- size, headers, CORS


async def test_body_size_limit(base: Settings, store: InvestigationStore) -> None:
    ctx = make_context(with_api(base, max_body_bytes=1_024), store)

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(4):
            yield b'{"question": "' + b"a" * 400

    async with api_client(ctx) as client:
        big = await client.post("/api/investigations", json={"question": "a" * 2_000})
        chunked = await client.post(
            "/api/investigations", content=chunks(), headers={"content-type": "application/json"}
        )
        small = await client.post("/api/investigations", json={"question": "x", "scenario": "S9"})
    assert big.status_code == 413 and big.json()["error"]["code"] == "payload_too_large"
    assert chunked.status_code == 413
    assert small.status_code == 404


async def test_security_headers(base: Settings, store: InvestigationStore) -> None:
    ctx = make_context(base, store)
    async with api_client(ctx) as client:
        health = await client.get("/api/health")
        docs = await client.get("/api/docs")
        missing = await client.get("/api/investigations/inv-nope")
    for response in (health, missing):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert "default-src 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
    assert "cdn.jsdelivr.net" in docs.headers["content-security-policy"]


def test_cors_origins_must_be_exact() -> None:
    for bad in ("*", "http://localhost:3100/app", "localhost:3100", "http://*.example.com"):
        with pytest.raises(ValidationError, match="exact origins"):
            ApiConfig(cors_origins=[bad])
    assert ApiConfig(cors_origins="https://aiops.example.com").cors_origins == [  # type: ignore[arg-type]
        "https://aiops.example.com"
    ]


# --------------------------------------------------------------------------- idempotency


async def test_idempotency_key(base: Settings, store: InvestigationStore) -> None:
    ctx = make_context(base, store)
    body = {"question": "x", "scenario": "S1"}
    key = {"Idempotency-Key": f"retry-{uuid4().hex}"}
    async with api_client(ctx) as client:
        first = await client.post("/api/investigations", json=body, headers=key)
        again = await client.post("/api/investigations", json=body, headers=key)
        other = await client.post(
            "/api/investigations", json={**body, "question": "y"}, headers=key
        )
        invalid = await client.post(
            "/api/investigations", json=body, headers={"Idempotency-Key": "no spaces!"}
        )
        await wait_until_idle(ctx)
        listed = (await client.get("/api/investigations")).json()["items"]
    assert first.status_code == again.status_code == 202
    assert again.json()["id"] == first.json()["id"]
    assert again.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert other.status_code == 422 and other.json()["error"]["code"] == "idempotency_key_reused"
    assert invalid.status_code == 400
    assert [i["id"] for i in listed] == [first.json()["id"]]  # one investigation, not two
