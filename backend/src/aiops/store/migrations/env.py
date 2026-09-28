"""Alembic environment: always run programmatically by ``aiops.store.db.upgrade``."""

from __future__ import annotations

from alembic import context

from aiops.store.tables import metadata

connection = context.config.attributes["connection"]
context.configure(
    connection=connection,
    target_metadata=metadata,
    version_table_schema=context.config.attributes.get("schema"),
)
with context.begin_transaction():
    context.run_migrations()
