"""InvestigationStore: persist investigations + their event log (SQLAlchemy 2, async).

``save`` writes the full Investigation JSON (the source of truth for ``aiops show`` and
the API) plus normalized rows (steps, tool calls, findings, evidence excerpts,
hypotheses, recommendations) for lists, filters and the dashboard. Investigations
therefore survive restarts; ``events`` replays the SSE stream of a stored investigation.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from aiops.core.config import Settings
from aiops.core.events import InvestigationEvent
from aiops.core.models import Investigation
from aiops.store import tables as t
from aiops.store.db import async_engine, database_url, upgrade

EXCERPT_CHARS = 1_500


def excerpt(data: dict[str, Any]) -> str | None:
    """A bounded excerpt of evidence data (the store never keeps raw logs)."""
    if not data:
        return None
    encoded = json.dumps(data, default=str, sort_keys=True)
    return encoded if len(encoded) <= EXCERPT_CHARS else encoded[: EXCERPT_CHARS - 3] + "..."


def summary_of(inv: Investigation) -> dict[str, Any]:
    """``InvestigationSummary`` of docs/api/contract.md (the list view)."""
    report = inv.report
    return {
        "id": inv.id,
        "incident": inv.incident.model_dump(mode="json"),
        "status": inv.status.value,
        "report": {
            "summary": report.summary if report else None,
            "severity": report.severity if report else None,
            "confidence": report.confidence if report else None,
        },
        "affected_services": report.affected_services if report else [],
        "created_at": inv.created_at.isoformat(),
        "completed_at": inv.completed_at.isoformat() if inv.completed_at else None,
        "duration_ms": inv.duration_ms,
        "mode": inv.mode,
    }


def _signals(inv: Investigation) -> list[str]:
    return list(dict.fromkeys(s for r in inv.results for s in r.signals))


class InvestigationStore:
    def __init__(self, url: str, schema: str = "investigations") -> None:
        self.url = url
        self.schema = schema
        self.engine: AsyncEngine = async_engine(url, schema)

    @classmethod
    def from_settings(cls, settings: Settings) -> InvestigationStore:
        return cls(database_url(settings), settings.storage.db_schema)

    def migrate(self) -> None:
        """Create/upgrade the tables (sync; call before first use)."""
        upgrade(self.url, self.schema)

    async def close(self) -> None:
        await self.engine.dispose()

    # -- writes ------------------------------------------------------------------------------

    async def save(self, inv: Investigation) -> None:
        """Insert or replace one investigation and its normalized rows."""
        report = inv.report
        top = next(
            (h for h in inv.hypotheses if report and h.id == report.root_cause_hypothesis_id), None
        )
        row = {
            "id": inv.id,
            "incident_id": inv.incident.id,
            "title": inv.incident.title,
            "question": inv.context.question if inv.context else inv.incident.title,
            "service": inv.context.service if inv.context else inv.incident.service,
            "environment": inv.context.environment if inv.context else inv.incident.environment,
            "status": inv.status.value,
            "severity": report.severity if report else None,
            "confidence": report.confidence if report else None,
            "summary": report.summary if report else None,
            "root_cause": top.statement if top else None,
            "mode": inv.mode,
            "created_at": inv.created_at,
            "completed_at": inv.completed_at,
            "duration_ms": inv.duration_ms,
            "input_tokens": inv.usage.input_tokens,
            "output_tokens": inv.usage.output_tokens,
            "llm_calls": inv.usage.calls,
            "signals": _signals(inv),
            "document": inv.model_dump(mode="json"),
        }
        step_of = {r.task_id: r for r in inv.results}
        async with self.engine.begin() as conn:
            await self._delete_rows(conn, [inv.id], keep_events=True)
            await conn.execute(sa.insert(t.investigation), [row])
            steps = []
            for s in inv.steps:
                result = step_of.get(s.id)
                steps.append(
                    {
                        "id": s.id,
                        "investigation_id": inv.id,
                        "agent": s.agent,
                        "objective": s.objective,
                        "round": s.round,
                        "status": s.status.value,
                        "result_status": result.status.value if result else None,
                        "summary": result.summary if result else None,
                        "signals": result.signals if result else [],
                        "started_at": s.started_at,
                        "finished_at": s.finished_at,
                        "duration_ms": result.duration_ms if result else None,
                        "tokens": result.usage.total_tokens if result else 0,
                    }
                )
            await self._insert(conn, t.step, steps)
            await self._insert(
                conn,
                t.tool_call,
                [
                    {
                        "id": c.id,
                        "investigation_id": inv.id,
                        "step_id": r.task_id,
                        "agent": c.agent,
                        "capability": c.capability,
                        "tool": c.tool,
                        "status": c.status,
                        "arguments": c.arguments,
                        "started_at": c.started_at,
                        "duration_ms": c.duration_ms,
                        "error": c.error,
                    }
                    for r in inv.results
                    for c in r.tool_calls
                ],
            )
            await self._insert(
                conn,
                t.finding,
                [
                    {
                        "id": f.id,
                        "investigation_id": inv.id,
                        "step_id": r.task_id,
                        "agent": r.agent,
                        "kind": f.kind.value,
                        "type": f.type,
                        "description": f.description,
                        "evidence_ids": f.evidence_ids,
                        "confidence": f.confidence,
                    }
                    for r in inv.results
                    for f in r.findings
                ],
            )
            await self._insert(
                conn,
                t.evidence,
                [
                    {
                        "id": e.id,
                        "investigation_id": inv.id,
                        "step_id": r.task_id,
                        "agent": r.agent,
                        "kind": e.kind.value,
                        "source": e.source,
                        "summary": e.summary,
                        "link": e.link,
                        "timestamp": e.timestamp,
                        "query": e.query,
                        "excerpt": excerpt(e.data),
                    }
                    for r in inv.results
                    for e in r.evidence
                ],
            )
            await self._insert(
                conn,
                t.hypothesis,
                [
                    {
                        "id": h.id,
                        "investigation_id": inv.id,
                        "rank": rank,
                        "statement": h.statement,
                        "confidence": h.confidence,
                        "supporting_evidence_ids": h.supporting_evidence_ids,
                        "contradicting_evidence_ids": h.contradicting_evidence_ids,
                    }
                    for rank, h in enumerate(inv.hypotheses, start=1)
                ],
            )
            await self._insert(
                conn,
                t.recommendation,
                [
                    {
                        "id": rc.id,
                        "investigation_id": inv.id,
                        "action": rc.action,
                        "rationale": rc.rationale,
                        "risk": rc.risk,
                        "requires_approval": rc.requires_approval,
                        "evidence_ids": rc.evidence_ids,
                    }
                    for rc in inv.recommendations
                ],
            )

    async def save_events(self, events: Sequence[InvestigationEvent]) -> None:
        """Append events (the SSE log); already stored ``seq`` numbers are replaced."""
        if not events:
            return
        async with self.engine.begin() as conn:
            for inv_id in {e.investigation_id for e in events}:
                seqs = [e.seq for e in events if e.investigation_id == inv_id]
                await conn.execute(
                    sa.delete(t.event).where(
                        t.event.c.investigation_id == inv_id, t.event.c.seq.in_(seqs)
                    )
                )
            await conn.execute(
                sa.insert(t.event),
                [
                    {
                        "investigation_id": e.investigation_id,
                        "seq": e.seq,
                        "type": e.type,
                        "agent": e.agent,
                        "timestamp": e.timestamp,
                        "data": e.model_dump(mode="json")["data"],
                    }
                    for e in events
                ],
            )

    async def link_approval(
        self, approval_id: str, investigation_id: str, action: str, at: datetime
    ) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(
                sa.delete(t.approval_link).where(
                    t.approval_link.c.approval_id == approval_id,
                    t.approval_link.c.investigation_id == investigation_id,
                )
            )
            await conn.execute(
                sa.insert(t.approval_link),
                [
                    {
                        "approval_id": approval_id,
                        "investigation_id": investigation_id,
                        "action": action,
                        "created_at": at,
                    }
                ],
            )

    async def delete(self, ids: Sequence[str]) -> None:
        async with self.engine.begin() as conn:
            await self._delete_rows(conn, list(ids), keep_events=False)

    async def delete_mode(self, mode: str) -> int:
        """Delete every investigation of one ``mode`` (e.g. re-seeding demo data)."""
        async with self.engine.connect() as conn:
            ids = [
                r[0]
                for r in await conn.execute(
                    sa.select(t.investigation.c.id).where(t.investigation.c.mode == mode)
                )
            ]
        await self.delete(ids)
        return len(ids)

    @staticmethod
    async def _insert(conn: Any, table: sa.Table, rows: list[dict[str, Any]]) -> None:
        if rows:
            await conn.execute(sa.insert(table), rows)

    @staticmethod
    async def _delete_rows(conn: Any, ids: list[str], *, keep_events: bool) -> None:
        if not ids:
            return
        children = [t.step, t.tool_call, t.finding, t.evidence, t.hypothesis, t.recommendation]
        if not keep_events:
            children += [t.event, t.approval_link]
        for table in children:
            await conn.execute(sa.delete(table).where(table.c.investigation_id.in_(ids)))
        await conn.execute(sa.delete(t.investigation).where(t.investigation.c.id.in_(ids)))

    # -- reads -------------------------------------------------------------------------------

    async def get(self, investigation_id: str) -> Investigation | None:
        async with self.engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(t.investigation.c.document).where(
                        t.investigation.c.id == investigation_id
                    )
                )
            ).first()
        return Investigation.model_validate(row[0]) if row else None

    async def search(
        self,
        *,
        status: str | None = None,
        service: str | None = None,
        severity: str | None = None,
        mode: str | None = None,
        q: str | None = None,
        limit: int = 50,
        before: datetime | None = None,
        since: datetime | None = None,
    ) -> list[Investigation]:
        """Newest first. ``before`` = cursor (created_at of the last item of a page)."""
        query = sa.select(t.investigation.c.document).order_by(
            t.investigation.c.created_at.desc(), t.investigation.c.id.desc()
        )
        column = t.investigation.c
        for value, col in (
            (status, column.status),
            (service, column.service),
            (severity, column.severity),
            (mode, column.mode),
        ):
            if value:
                query = query.where(col == value)
        if q:
            like = f"%{q.casefold()}%"
            query = query.where(
                sa.or_(
                    sa.func.lower(column.title).like(like),
                    sa.func.lower(sa.func.coalesce(column.summary, "")).like(like),
                    sa.func.lower(sa.func.coalesce(column.root_cause, "")).like(like),
                )
            )
        if before is not None:
            query = query.where(column.created_at < before)
        if since is not None:
            query = query.where(column.created_at >= since)
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query.limit(limit))).all()
        return [Investigation.model_validate(r[0]) for r in rows]

    async def events(self, investigation_id: str, after_seq: int = 0) -> list[InvestigationEvent]:
        query = (
            sa.select(t.event)
            .where(t.event.c.investigation_id == investigation_id, t.event.c.seq > after_seq)
            .order_by(t.event.c.seq)
        )
        async with self.engine.connect() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [
            InvestigationEvent(
                type=r["type"],
                investigation_id=r["investigation_id"],
                seq=r["seq"],
                agent=r["agent"],
                timestamp=r["timestamp"],
                data=r["data"] or {},
            )
            for r in rows
        ]

    async def count(self) -> int:
        async with self.engine.connect() as conn:
            value = (
                await conn.execute(sa.select(sa.func.count()).select_from(t.investigation))
            ).scalar()
        return int(value or 0)
