"""Database URLs, engines and migrations for the evidence store.

Postgres (the compose ``aiops-postgres``) in normal use; SQLite in unit tests. Tables are
defined without a schema and mapped into ``storage.db_schema`` on Postgres.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from aiops.core.config import Settings

DATABASE_URL_VAR = "AIOPS_DATABASE_URL"
MIGRATIONS = Path(__file__).with_name("migrations")
#: Postgres advisory lock key that serializes concurrent `upgrade()` calls ("aiops-db").
_LOCK = 0x6169_6F70_732D_6462


class StoreError(Exception):
    """The store is unreachable or misconfigured. Message is for humans."""


def default_database_url() -> str:
    """``$AIOPS_DATABASE_URL``, else the local stack's POSTGRES_* settings."""
    url = os.environ.get(DATABASE_URL_VAR, "").strip()
    if url:
        return url
    user = quote(os.environ.get("POSTGRES_USER") or "aiops")
    password = quote(os.environ.get("POSTGRES_PASSWORD") or "aiops-local-only")
    host = os.environ.get("POSTGRES_HOST") or "localhost"
    port = os.environ.get("POSTGRES_PORT") or "15432"
    db = os.environ.get("POSTGRES_DB") or "aiops"
    return f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"


def database_url(settings: Settings) -> str:
    return settings.storage.database_url or default_database_url()


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _normalize(url: str) -> str:
    """``postgresql://`` / ``postgres://`` -> the psycopg 3 driver (sync and async)."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def sync_url(url: str) -> str:
    url = _normalize(url)
    return url.replace("sqlite+aiosqlite://", "sqlite://", 1) if is_sqlite(url) else url


def async_url(url: str) -> str:
    url = _normalize(url)
    if url.startswith("sqlite://"):
        return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
    return url


def execution_options(url: str, schema: str) -> dict[str, Any]:
    return {} if is_sqlite(url) else {"schema_translate_map": {None: schema}}


def sync_engine(url: str, schema: str) -> sa.Engine:
    return sa.create_engine(sync_url(url), execution_options=execution_options(url, schema))


def async_engine(url: str, schema: str) -> AsyncEngine:
    return create_async_engine(async_url(url), execution_options=execution_options(url, schema))


def upgrade(url: str, schema: str) -> None:
    """Create/upgrade the store's tables (Alembic ``upgrade head``). Idempotent."""
    engine = sa.create_engine(sync_url(url))
    try:
        with engine.begin() as connection:
            if not is_sqlite(url):
                # One migrator at a time (PR-047): the Helm migration Job and API replicas
                # starting together would otherwise race on CREATE TABLE. Released at commit.
                connection.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": _LOCK})
                connection.execute(sa.text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
            connection = connection.execution_options(**execution_options(url, schema))
            config = Config()
            config.set_main_option("script_location", str(MIGRATIONS))
            config.attributes["connection"] = connection
            config.attributes["schema"] = None if is_sqlite(url) else schema
            command.upgrade(config, "head")
    except sa.exc.OperationalError as exc:
        raise StoreError(
            f"Evidence store unreachable ({sync_url(url).split('@')[-1]}): {exc.orig}. "
            f"Is the stack up (make infra-up)? Override with {DATABASE_URL_VAR}."
        ) from exc
    finally:
        engine.dispose()
