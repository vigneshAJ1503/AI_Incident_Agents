from __future__ import annotations

import json

import pytest

from aiops.core.guardrails.redaction import SUPPORTED_KINDS, Redactor

ALL = sorted(SUPPORTED_KINDS)

# Built at runtime so no token-shaped literal lives in the source (keeps secret scanners strict).
FAKE_JWT = ".".join(["eyJ" + "hbGciOiJub25lIn0", "eyJ" + "zdWIiOiJ0ZXN0In0", "c2lnbmF0dXJl"])


@pytest.mark.parametrize(
    ("raw", "must_not_contain", "kind"),
    [
        ("user alice@example.com failed login", "alice@example.com", "emails"),
        ("client 10.42.0.17 timed out", "10.42.0.17", "ip_addresses"),
        (
            "token " + FAKE_JWT,
            "eyJhbGci",
            "jwt",
        ),
        ("Authorization: Bearer abcdef1234567890", "abcdef1234567890", "bearer_tokens"),
        ("using key sk-proj-abcdefghijklmnop1234", "sk-proj-abcdefghijklmnop1234", "api_keys"),
        ("db password=hunter2secret host=db", "hunter2secret", "api_keys"),
        ("card 4111 1111 1111 1111 declined", "4111 1111 1111 1111", "credit_cards"),
    ],
)
def test_redacts(raw: str, must_not_contain: str, kind: str) -> None:
    redactor = Redactor(ALL)
    out = redactor.text(raw)
    assert must_not_contain not in out
    assert f"[REDACTED:{kind}]" in out
    assert redactor.counts[kind] == 1


def test_non_luhn_numbers_are_kept() -> None:
    redactor = Redactor(ALL)
    assert redactor.text("order 1234567890123 total") == "order 1234567890123 total"


def test_only_configured_kinds() -> None:
    assert Redactor(["emails"]).text("10.0.0.1 a@b.io") == "10.0.0.1 [REDACTED:emails]"


def test_json_data_stays_valid_and_numbers_untouched() -> None:
    data = {
        "ts": 1758796200000,
        "msg": "card 4111111111111111 for bob@corp.com",
        "hits": [{"ip": "10.0.0.9"}],
    }
    out = Redactor(ALL).data(data)
    json.dumps(out)
    assert out["ts"] == 1758796200000
    assert out["msg"] == "card [REDACTED:credit_cards] for [REDACTED:emails]"
    assert out["hits"][0]["ip"] == "[REDACTED:ip_addresses]"


def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown"):
        Redactor(["phone_numbers"])
