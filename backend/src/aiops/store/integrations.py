"""Integration overrides + their audit trail in the evidence store (PR-046).

One row per ``(profile, capability)`` holds what the Web UI changed on top of the profile's
YAML; secrets are stored only as Fernet ciphertext (``core/secrets.py``). Every change adds
an ``integration_audit`` row naming the actor and the changed fields, never a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from aiops.core.integrations import FieldChange, IntegrationOverride, StoredSecret
from aiops.core.models import new_id, utcnow
from aiops.store import tables as t


@dataclass(frozen=True)
class AuditEntry:
    id: str
    recorded_at: datetime
    profile: str
    capability: str
    actor: str
    action: str  # update | reset
    changes: tuple[FieldChange, ...]


def _row_to_override(row: Any) -> IntegrationOverride:
    doc = dict(row.document or {})
    secrets = {
        str(name): StoredSecret(str(item["ciphertext"]), item.get("hint"))
        for name, item in (row.secrets or {}).items()
        if isinstance(item, dict) and item.get("ciphertext")
    }
    return IntegrationOverride(
        profile=row.profile,
        capability=row.capability,
        enabled=doc.get("enabled"),
        provider=doc.get("provider"),
        fields=dict(doc.get("fields") or {}),
        secrets=secrets,
        updated_at=row.updated_at,
        updated_by=row.updated_by,
    )


class IntegrationStore:
    """Async repository on the evidence store's engine (Postgres; SQLite in tests)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine

    async def overrides(self, profile: str) -> dict[str, IntegrationOverride]:
        query = sa.select(t.integration_override).where(t.integration_override.c.profile == profile)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return {row.capability: _row_to_override(row) for row in rows}

    async def save(
        self, override: IntegrationOverride, actor: str, action: str, changes: list[FieldChange]
    ) -> AuditEntry:
        """Upsert (or delete, when nothing is overridden any more) + one audit row, in one
        transaction."""
        now = utcnow()
        table = t.integration_override
        key = (table.c.profile == override.profile) & (table.c.capability == override.capability)
        entry = AuditEntry(
            id=new_id("iaud"),
            recorded_at=now,
            profile=override.profile,
            capability=override.capability,
            actor=actor,
            action=action,
            changes=tuple(changes),
        )
        async with self.engine.begin() as conn:
            await conn.execute(sa.delete(table).where(key))
            if not override.empty:
                await conn.execute(
                    sa.insert(table).values(
                        profile=override.profile,
                        capability=override.capability,
                        document=override.document(),
                        secrets={
                            name: {"ciphertext": s.ciphertext, "hint": s.hint}
                            for name, s in override.secrets.items()
                        },
                        updated_at=now,
                        updated_by=actor,
                    )
                )
            await conn.execute(
                sa.insert(t.integration_audit).values(
                    id=entry.id,
                    recorded_at=now,
                    profile=entry.profile,
                    capability=entry.capability,
                    actor=actor,
                    action=action,
                    changes=[{"field": c.field, "change": c.change} for c in changes],
                )
            )
        override.updated_at, override.updated_by = now, actor
        return entry

    async def audit(
        self, profile: str, capability: str | None = None, limit: int = 50
    ) -> list[AuditEntry]:
        table = t.integration_audit
        query = sa.select(table).where(table.c.profile == profile)
        if capability:
            query = query.where(table.c.capability == capability)
        query = query.order_by(table.c.recorded_at.desc(), table.c.id.desc()).limit(limit)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [
            AuditEntry(
                id=row.id,
                recorded_at=row.recorded_at,
                profile=row.profile,
                capability=row.capability,
                actor=row.actor,
                action=row.action,
                changes=tuple(
                    FieldChange(str(c.get("field", "")), c.get("change", "updated"))
                    for c in (row.changes or [])
                ),
            )
            for row in rows
        ]
