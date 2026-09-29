"""Settings from the environment (the Helm chart passes one database URL Secret, PR-047)."""

from __future__ import annotations

import pytest

from mock_tickets_mcp.config import ServerSettings


def test_dsn_from_pg_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TICKETS_DATABASE_URL", raising=False)
    monkeypatch.setenv("PG_HOST", "db")
    monkeypatch.setenv("PG_PORT", "5432")
    monkeypatch.setenv("PG_USER", "aiops")
    monkeypatch.setenv("PG_PASSWORD", "p@ss word")
    assert ServerSettings.from_env().dsn == "postgresql://aiops:p%40ss%20word@db:5432/aiops"


@pytest.mark.parametrize(
    ("url", "dsn"),
    [
        ("postgresql://u:p@ext:5432/tickets", "postgresql://u:p@ext:5432/tickets"),
        ("postgresql+psycopg://u:p@ext:5432/tickets", "postgresql://u:p@ext:5432/tickets"),
    ],
)
def test_database_url_wins(monkeypatch: pytest.MonkeyPatch, url: str, dsn: str) -> None:
    monkeypatch.setenv("PG_HOST", "ignored")
    monkeypatch.setenv("TICKETS_DATABASE_URL", url)
    settings = ServerSettings.from_env()
    assert settings.dsn == dsn
    assert "u:p" not in repr(settings)  # never logged
