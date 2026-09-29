"""Integration overrides from the Web UI (PR-046): ``integration_override`` (a per-profile,
per-capability overlay on the YAML profile, secrets encrypted) and ``integration_audit``
(who changed which field; never a value).

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "integration_override",
        sa.Column("profile", sa.String(100), primary_key=True),
        sa.Column("capability", sa.String(64), primary_key=True),
        sa.Column("document", sa.JSON(), nullable=False),
        sa.Column("secrets", sa.JSON(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("updated_by", sa.String(200), nullable=False),
    )
    op.create_table(
        "integration_audit",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("recorded_at", TS, nullable=False),
        sa.Column("profile", sa.String(100), nullable=False),
        sa.Column("capability", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(200), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
    )
    op.create_index("ix_integration_audit_recorded_at", "integration_audit", ["recorded_at"])
    op.create_index("ix_integration_audit_capability", "integration_audit", ["capability"])


def downgrade() -> None:
    op.drop_table("integration_audit")
    op.drop_table("integration_override")
