"""Initial evidence store (PR-032): investigation, step, tool_call, finding, evidence,
hypothesis, recommendation, event log, approval (+ link, audit) and tool-call audit.

The initial revision creates the tables from ``aiops.store.tables`` as of this revision;
later revisions change them with explicit ``op.*`` calls.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

from aiops.store.tables import metadata

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    metadata.drop_all(bind=op.get_bind())
