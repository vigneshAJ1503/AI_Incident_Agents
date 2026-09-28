"""Postgres implementations of the existing sync interfaces (PR-032):

* ``SqlApprovalStore``      -> ``ApprovalStore`` (save/get/list); every proposal tied to an
  investigation also gets an ``approval_link`` row;
* ``SqlApprovalAuditSink``  -> ``ApprovalAuditSink``;
* ``SqlAuditSink``          -> ``AuditSink`` (tool-call audit), falling back to the JSONL
  file when the database is unavailable (an audit record is never lost).

Selected with ``storage.approvals: postgres`` / ``storage.audit: postgres``; the JSON/JSONL
files stay the default and the fallback.
"""

from __future__ import annotations

import logging

import sqlalchemy as sa

from aiops.core.guardrails.approvals import ActionProposal, ActionStatus, ApprovalAuditRecord
from aiops.core.guardrails.audit import AuditRecord, AuditSink
from aiops.store import tables as t
from aiops.store.db import sync_engine, upgrade

log = logging.getLogger(__name__)


class _Sql:
    def __init__(self, url: str, schema: str = "investigations", *, migrate: bool = True) -> None:
        if migrate:
            upgrade(url, schema)
        self.engine = sync_engine(url, schema)


class SqlApprovalStore(_Sql):
    def save(self, proposal: ActionProposal) -> None:
        with self.engine.begin() as conn:
            conn.execute(sa.delete(t.approval).where(t.approval.c.id == proposal.id))
            conn.execute(
                sa.insert(t.approval),
                [
                    {
                        "id": proposal.id,
                        "status": proposal.status.value,
                        "action": proposal.action,
                        "capability": proposal.capability,
                        "tool": proposal.tool,
                        "investigation_id": proposal.investigation_id,
                        "created_at": proposal.created_at,
                        "document": proposal.model_dump(mode="json"),
                    }
                ],
            )
            if proposal.investigation_id:
                link = t.approval_link.c
                exists = conn.execute(
                    sa.select(link.approval_id).where(
                        link.approval_id == proposal.id,
                        link.investigation_id == proposal.investigation_id,
                    )
                ).first()
                if not exists:
                    conn.execute(
                        sa.insert(t.approval_link),
                        [
                            {
                                "approval_id": proposal.id,
                                "investigation_id": proposal.investigation_id,
                                "action": proposal.action,
                                "created_at": proposal.created_at,
                            }
                        ],
                    )

    def get(self, proposal_id: str) -> ActionProposal | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(t.approval.c.document).where(t.approval.c.id == proposal_id)
            ).first()
        return ActionProposal.model_validate(row[0]) if row else None

    def list(self, status: ActionStatus | None = None) -> list[ActionProposal]:
        query = sa.select(t.approval.c.document).order_by(t.approval.c.created_at)
        if status is not None:
            query = query.where(t.approval.c.status == status.value)
        with self.engine.connect() as conn:
            return [ActionProposal.model_validate(r[0]) for r in conn.execute(query)]


class SqlApprovalAuditSink(_Sql):
    def record(self, record: ApprovalAuditRecord) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                sa.insert(t.approval_audit),
                [
                    {
                        "id": record.id,
                        "recorded_at": record.recorded_at,
                        "proposal_id": record.proposal_id,
                        "investigation_id": record.investigation_id,
                        "document": record.model_dump(mode="json"),
                    }
                ],
            )


class SqlAuditSink:
    """Tool-call audit in Postgres; JSONL fallback on any database error."""

    def __init__(self, url: str, schema: str, fallback: AuditSink) -> None:
        self.fallback = fallback
        self.engine: sa.Engine | None
        try:
            upgrade(url, schema)
            self.engine = sync_engine(url, schema)
        except Exception as exc:  # the audit trail must never break an investigation
            log.warning("Audit store unavailable, using the JSONL fallback: %s", exc)
            self.engine = None

    def record(self, record: AuditRecord) -> None:
        if self.engine is not None:
            call = record.tool_call
            try:
                with self.engine.begin() as conn:
                    conn.execute(
                        sa.insert(t.audit),
                        [
                            {
                                "id": record.id,
                                "recorded_at": record.recorded_at,
                                "request_id": record.request_id,
                                "investigation_id": record.investigation_id,
                                "agent": call.agent,
                                "capability": call.capability,
                                "tool": call.tool,
                                "status": call.status,
                                "document": record.model_dump(mode="json"),
                            }
                        ],
                    )
                return
            except Exception as exc:
                log.warning("Audit insert failed, writing to the JSONL fallback: %s", exc)
        self.fallback.record(record)
