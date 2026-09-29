"""Evidence store tables (PR-032). Schema-less here; Postgres maps them into
``storage.db_schema`` (default ``investigations``) with ``schema_translate_map``.

The full ``Investigation`` JSON (``investigation.document``) is the source of truth for
``aiops show`` and the API; the normalized tables make lists, filters and the dashboard
cheap. Evidence rows keep **excerpts** (``EXCERPT_CHARS``), never raw logs. Child rows are
keyed by ``(investigation_id, id)``.
"""

from __future__ import annotations

import sqlalchemy as sa

metadata = sa.MetaData()

JSON = sa.JSON()
TS = sa.DateTime(timezone=True)

investigation = sa.Table(
    "investigation",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("incident_id", sa.String(64), nullable=False),
    sa.Column("title", sa.Text, nullable=False),
    sa.Column("question", sa.Text, nullable=False, server_default=""),
    sa.Column("service", sa.String(200), index=True),
    sa.Column("environment", sa.String(100)),
    sa.Column("status", sa.String(32), nullable=False, index=True),
    sa.Column("severity", sa.String(16), index=True),
    sa.Column("confidence", sa.Float),
    sa.Column("summary", sa.Text),
    sa.Column("root_cause", sa.Text),
    sa.Column("mode", sa.String(16), nullable=False, server_default="live"),
    sa.Column("created_at", TS, nullable=False, index=True),
    sa.Column("completed_at", TS),
    sa.Column("duration_ms", sa.Float),
    sa.Column("input_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("output_tokens", sa.Integer, nullable=False, server_default="0"),
    sa.Column("llm_calls", sa.Integer, nullable=False, server_default="0"),
    sa.Column("signals", JSON),
    sa.Column("document", JSON, nullable=False),
)

step = sa.Table(
    "step",
    metadata,
    sa.Column("investigation_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("agent", sa.String(64), nullable=False),
    sa.Column("objective", sa.Text, nullable=False),
    sa.Column("round", sa.Integer, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("result_status", sa.String(16)),
    sa.Column("summary", sa.Text),
    sa.Column("signals", JSON),
    sa.Column("started_at", TS),
    sa.Column("finished_at", TS),
    sa.Column("duration_ms", sa.Float),
    sa.Column("tokens", sa.Integer, nullable=False, server_default="0"),
)

tool_call = sa.Table(
    "tool_call",
    metadata,
    sa.Column("investigation_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("step_id", sa.String(64)),
    sa.Column("agent", sa.String(64), nullable=False),
    sa.Column("capability", sa.String(64), nullable=False),
    sa.Column("tool", sa.String(128), nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("arguments", JSON),
    sa.Column("started_at", TS),
    sa.Column("duration_ms", sa.Float),
    sa.Column("error", sa.Text),
)

finding = sa.Table(
    "finding",
    metadata,
    sa.Column("investigation_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("step_id", sa.String(64)),
    sa.Column("agent", sa.String(64), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("type", sa.String(128), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("evidence_ids", JSON),
    sa.Column("confidence", sa.Float),
)

evidence = sa.Table(
    "evidence",
    metadata,
    sa.Column("investigation_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("step_id", sa.String(64)),
    sa.Column("agent", sa.String(64), nullable=False),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("source", sa.String(200), nullable=False),
    sa.Column("summary", sa.Text, nullable=False),
    sa.Column("link", sa.Text),
    sa.Column("timestamp", TS),
    sa.Column("query", sa.Text),
    sa.Column("excerpt", sa.Text),  # references + a relevant excerpt, never raw logs
)

hypothesis = sa.Table(
    "hypothesis",
    metadata,
    sa.Column("investigation_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("rank", sa.Integer, nullable=False),
    sa.Column("statement", sa.Text, nullable=False),
    sa.Column("confidence", sa.Float, nullable=False),
    sa.Column("supporting_evidence_ids", JSON),
    sa.Column("contradicting_evidence_ids", JSON),
)

recommendation = sa.Table(
    "recommendation",
    metadata,
    sa.Column("investigation_id", sa.String(64), primary_key=True),
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("action", sa.Text, nullable=False),
    sa.Column("rationale", sa.Text),
    sa.Column("risk", sa.String(16)),
    sa.Column("requires_approval", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("evidence_ids", JSON),
)

event = sa.Table(
    "event",
    metadata,
    sa.Column("investigation_id", sa.String(64), primary_key=True),
    sa.Column("seq", sa.Integer, primary_key=True),
    sa.Column("type", sa.String(32), nullable=False),
    sa.Column("agent", sa.String(64)),
    sa.Column("timestamp", TS, nullable=False),
    sa.Column("data", JSON),
)

approval = sa.Table(
    "approval",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("status", sa.String(16), nullable=False, index=True),
    sa.Column("action", sa.String(64), nullable=False),
    sa.Column("capability", sa.String(64), nullable=False),
    sa.Column("tool", sa.String(128), nullable=False),
    sa.Column("investigation_id", sa.String(64), index=True),
    sa.Column("created_at", TS, nullable=False),
    sa.Column("document", JSON, nullable=False),
)

approval_link = sa.Table(
    "approval_link",
    metadata,
    sa.Column("approval_id", sa.String(64), primary_key=True),
    sa.Column("investigation_id", sa.String(64), primary_key=True),
    sa.Column("action", sa.String(64), nullable=False),
    sa.Column("created_at", TS, nullable=False),
)

approval_audit = sa.Table(
    "approval_audit",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("recorded_at", TS, nullable=False),
    sa.Column("proposal_id", sa.String(64), nullable=False, index=True),
    sa.Column("investigation_id", sa.String(64)),
    sa.Column("document", JSON, nullable=False),
)

audit = sa.Table(
    "audit",
    metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("recorded_at", TS, nullable=False),
    sa.Column("request_id", sa.String(64), nullable=False),
    sa.Column("investigation_id", sa.String(64), index=True),
    sa.Column("agent", sa.String(64)),
    sa.Column("capability", sa.String(64)),
    sa.Column("tool", sa.String(128)),
    sa.Column("status", sa.String(16)),
    sa.Column("document", JSON, nullable=False),
)

# --------------------------------------------------------------------------- integrations (PR-046)
# A separate MetaData: revision 0001 creates ``metadata`` wholesale, so tables added later
# must not be part of it (0002 creates these with explicit ``op.create_table`` calls).
integration_metadata = sa.MetaData()

#: Web UI overrides of one capability, layered over the active profile's YAML (the YAML
#: stays the source of defaults). ``document`` = non-secret overrides
#: ``{enabled?, provider?, fields: {dotted key: value}}``; ``secrets`` = ``{header name:
#: {ciphertext, hint}}`` encrypted with AIOPS_SECRETS_KEY (core/secrets.py).
integration_override = sa.Table(
    "integration_override",
    integration_metadata,
    sa.Column("profile", sa.String(100), primary_key=True),
    sa.Column("capability", sa.String(64), primary_key=True),
    sa.Column("document", JSON, nullable=False),
    sa.Column("secrets", JSON, nullable=False),
    sa.Column("updated_at", TS, nullable=False),
    sa.Column("updated_by", sa.String(200), nullable=False),
)

#: Who changed which field of which integration, and when. Never a value.
integration_audit = sa.Table(
    "integration_audit",
    integration_metadata,
    sa.Column("id", sa.String(64), primary_key=True),
    sa.Column("recorded_at", TS, nullable=False, index=True),
    sa.Column("profile", sa.String(100), nullable=False),
    sa.Column("capability", sa.String(64), nullable=False, index=True),
    sa.Column("actor", sa.String(200), nullable=False),
    sa.Column("action", sa.String(32), nullable=False),
    sa.Column("changes", JSON, nullable=False),
)
