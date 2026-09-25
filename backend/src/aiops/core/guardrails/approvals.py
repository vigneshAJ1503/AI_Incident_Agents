"""Human approval for every write action (MASTER_PLAN §16.2, UC-04).

Lifecycle, with an audit record at every transition:

    ActionProposal --policy check--> PENDING --human--> APPROVED --executor--> EXECUTED
                          |                   \\--human--> DENIED              \\--> FAILED
                          \\--> REJECTED        \\--ttl----> EXPIRED

* Agents never hold write tools: ``capabilities.<cap>.tool_allowlist`` (what agents get) and
  ``write_allowlist`` (what the executor may call) are disjoint by config validation.
* Only ``ApprovalExecutor`` calls write tools, only through ``MCPRegistry.write_toolset``,
  and only for an APPROVED, unexpired proposal that still passes the policy.
* Store: a JSON file under ``.data/`` behind ``ApprovalStore`` (Postgres in PR-032);
  ``MemoryApprovalStore`` for tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aiops.core.config import ConfigError, Settings
from aiops.core.models import new_id, utcnow

if TYPE_CHECKING:
    from aiops.mcp.registry import MCPRegistry

Risk = Literal["low", "medium", "high"]
Clock = Callable[[], datetime]


class ApprovalError(Exception):
    """An invalid request (unknown id, wrong state, missing approver). Message is for humans."""


class ActionStatus(StrEnum):
    PENDING = "pending"  # passed the policy, waiting for a human
    REJECTED = "rejected"  # failed the policy check; can never be approved
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"  # nobody decided within the TTL
    EXECUTED = "executed"
    FAILED = "failed"  # approved, but the execution failed


#: Allowed transitions (None = creation).
TRANSITIONS: dict[ActionStatus | None, set[ActionStatus]] = {
    None: {ActionStatus.PENDING, ActionStatus.REJECTED},
    ActionStatus.PENDING: {ActionStatus.APPROVED, ActionStatus.DENIED, ActionStatus.EXPIRED},
    ActionStatus.APPROVED: {ActionStatus.EXECUTED, ActionStatus.FAILED},
}


class Transition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: datetime
    from_status: ActionStatus | None
    to_status: ActionStatus
    actor: str
    note: str = ""


class ActionProposal(BaseModel):
    """A write the system would like to perform, pending a human decision."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("act"))
    action: str  # e.g. create_ticket, comment_ticket
    capability: str  # e.g. tickets
    tool: str  # e.g. jira_create_issue
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    risk: Risk = "medium"
    requested_by: str
    investigation_id: str | None = None
    status: ActionStatus = ActionStatus.PENDING
    policy_reason: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None
    decided_by: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = None
    executed_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    history: list[Transition] = Field(default_factory=list)

    @property
    def arguments_sha256(self) -> str:
        encoded = json.dumps(self.arguments, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def is_expired(self, now: datetime) -> bool:
        return (
            self.status is ActionStatus.PENDING
            and self.expires_at is not None
            and now >= self.expires_at
        )


# --------------------------------------------------------------------------- audit


class ApprovalAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("audit"))
    recorded_at: datetime = Field(default_factory=utcnow)
    proposal_id: str
    investigation_id: str | None = None
    action: str
    capability: str
    tool: str
    arguments_sha256: str  # arguments themselves stay in the store, not in the audit log
    from_status: ActionStatus | None
    to_status: ActionStatus
    actor: str
    note: str = ""


class ApprovalAuditSink(Protocol):
    def record(self, record: ApprovalAuditRecord) -> None: ...


class MemoryApprovalAuditSink:
    def __init__(self) -> None:
        self.records: list[ApprovalAuditRecord] = []

    def record(self, record: ApprovalAuditRecord) -> None:
        self.records.append(record)


class JsonlApprovalAuditSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, record: ApprovalAuditRecord) -> None:
        line = json.dumps(record.model_dump(mode="json"), sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as handle:
                handle.write(line + "\n")


# --------------------------------------------------------------------------- stores


class ApprovalStore(Protocol):
    def save(self, proposal: ActionProposal) -> None: ...

    def get(self, proposal_id: str) -> ActionProposal | None: ...

    def list(self, status: ActionStatus | None = None) -> list[ActionProposal]: ...


class MemoryApprovalStore:
    def __init__(self) -> None:
        self._items: dict[str, ActionProposal] = {}

    def save(self, proposal: ActionProposal) -> None:
        self._items[proposal.id] = proposal.model_copy(deep=True)

    def get(self, proposal_id: str) -> ActionProposal | None:
        found = self._items.get(proposal_id)
        return found.model_copy(deep=True) if found else None

    def list(self, status: ActionStatus | None = None) -> list[ActionProposal]:
        items = [p.model_copy(deep=True) for p in self._items.values()]
        return [p for p in items if status is None or p.status is status]


class JsonFileApprovalStore:
    """All proposals in one JSON file, rewritten atomically (tmp file + rename)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, ActionProposal]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except ValueError as exc:
            raise ConfigError(f"Approval store {self.path} is not valid JSON: {exc}") from exc
        items = [ActionProposal.model_validate(p) for p in raw.get("proposals", [])]
        return {p.id: p for p in items}

    def _write(self, items: dict[str, ActionProposal]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"proposals": [p.model_dump(mode="json") for p in items.values()]}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".approvals-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def save(self, proposal: ActionProposal) -> None:
        with self._lock:
            items = self._read()
            items[proposal.id] = proposal
            self._write(items)

    def get(self, proposal_id: str) -> ActionProposal | None:
        with self._lock:
            return self._read().get(proposal_id)

    def list(self, status: ActionStatus | None = None) -> list[ActionProposal]:
        with self._lock:
            items = list(self._read().values())
        return [p for p in items if status is None or p.status is status]


# --------------------------------------------------------------------------- policy


class ApprovalPolicy:
    """What may even be proposed. Returns a rejection reason, or None when allowed."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def check(self, proposal: ActionProposal) -> str | None:
        try:
            cap = self.settings.capability(proposal.capability)
        except ConfigError as exc:
            return str(exc)
        if proposal.tool not in cap.write_allowlist:
            allowed = ", ".join(cap.write_allowlist) or "none"
            return (
                f"tool '{proposal.tool}' is not in capabilities.{proposal.capability}."
                f"write_allowlist ({allowed})"
            )
        if proposal.tool in cap.tool_allowlist:  # also prevented by config validation
            return f"tool '{proposal.tool}' is readable by agents; refusing to treat it as a write"
        project = cap.settings.get("project_key")
        if project:
            target = proposal.arguments.get("project_key")
            if target is not None and str(target) != str(project):
                return f"project '{target}' is not the configured project '{project}'"
            key = proposal.arguments.get("issue_key")
            if key is not None and not str(key).startswith(f"{project}-"):
                return f"issue '{key}' is outside the configured project '{project}'"
        if not proposal.reason.strip():
            return "a reason is required"
        return None


# --------------------------------------------------------------------------- service


class ApprovalService:
    """Owns the lifecycle; every transition is validated, stored and audited."""

    def __init__(
        self,
        store: ApprovalStore,
        audit: ApprovalAuditSink,
        policy: ApprovalPolicy,
        *,
        ttl: timedelta = timedelta(hours=24),
        clock: Clock = utcnow,
    ) -> None:
        self.store = store
        self.audit = audit
        self.policy = policy
        self.ttl = ttl
        self.clock = clock

    @classmethod
    def from_settings(cls, settings: Settings, *, clock: Clock = utcnow) -> ApprovalService:
        guardrails = settings.guardrails
        return cls(
            JsonFileApprovalStore(settings.repo_path(guardrails.approvals_path)),
            JsonlApprovalAuditSink(settings.repo_path(guardrails.approvals_audit_path)),
            ApprovalPolicy(settings),
            ttl=timedelta(hours=guardrails.approval_ttl_hours),
            clock=clock,
        )

    def _transition(
        self, proposal: ActionProposal, to: ActionStatus, actor: str, note: str = ""
    ) -> ActionProposal:
        current = None if not proposal.history else proposal.status
        if to not in TRANSITIONS.get(current, set()):
            raise ApprovalError(
                f"{proposal.id} is {proposal.status.value}; it can't become {to.value}"
            )
        now = self.clock()
        proposal.history.append(
            Transition(at=now, from_status=current, to_status=to, actor=actor, note=note)
        )
        proposal.status = to
        self.store.save(proposal)
        self.audit.record(
            ApprovalAuditRecord(
                recorded_at=now,
                proposal_id=proposal.id,
                investigation_id=proposal.investigation_id,
                action=proposal.action,
                capability=proposal.capability,
                tool=proposal.tool,
                arguments_sha256=proposal.arguments_sha256,
                from_status=current,
                to_status=to,
                actor=actor,
                note=note,
            )
        )
        return proposal

    # -- creation --------------------------------------------------------------------------

    def propose(
        self,
        *,
        action: str,
        capability: str,
        tool: str,
        arguments: dict[str, Any],
        reason: str,
        requested_by: str,
        risk: Risk = "medium",
        investigation_id: str | None = None,
    ) -> ActionProposal:
        if not requested_by.strip():
            raise ApprovalError("requested_by is required")
        now = self.clock()
        proposal = ActionProposal(
            action=action,
            capability=capability,
            tool=tool,
            arguments=arguments,
            reason=reason,
            risk=risk,
            requested_by=requested_by,
            investigation_id=investigation_id,
            created_at=now,
            expires_at=now + self.ttl,
        )
        rejection = self.policy.check(proposal)
        if rejection:
            proposal.policy_reason = rejection
            return self._transition(proposal, ActionStatus.REJECTED, "policy", rejection)
        return self._transition(proposal, ActionStatus.PENDING, requested_by, "policy check passed")

    # -- queries ---------------------------------------------------------------------------

    def get(self, proposal_id: str) -> ActionProposal:
        proposal = self.store.get(proposal_id)
        if proposal is None:
            raise ApprovalError(f"No action proposal '{proposal_id}'")
        return self._expire_if_due(proposal)

    def proposals(self, status: ActionStatus | None = None) -> list[ActionProposal]:
        self.expire_stale()
        items = self.store.list(status)
        return sorted(items, key=lambda p: p.created_at, reverse=True)

    def _expire_if_due(self, proposal: ActionProposal) -> ActionProposal:
        if proposal.is_expired(self.clock()):
            return self._transition(
                proposal, ActionStatus.EXPIRED, "system", "approval TTL elapsed"
            )
        return proposal

    def expire_stale(self) -> list[ActionProposal]:
        return [
            self._transition(p, ActionStatus.EXPIRED, "system", "approval TTL elapsed")
            for p in self.store.list(ActionStatus.PENDING)
            if p.is_expired(self.clock())
        ]

    # -- human decisions -------------------------------------------------------------------

    def _decide(self, proposal_id: str, to: ActionStatus, actor: str, note: str) -> ActionProposal:
        if not actor.strip():
            raise ApprovalError("an approver identity is required")
        proposal = self.get(proposal_id)
        if proposal.status is not ActionStatus.PENDING:
            raise ApprovalError(f"{proposal.id} is {proposal.status.value}, not pending")
        proposal.decided_by = actor
        proposal.decided_at = self.clock()
        proposal.decision_note = note or None
        return self._transition(proposal, to, actor, note)

    def approve(self, proposal_id: str, actor: str, note: str = "") -> ActionProposal:
        return self._decide(proposal_id, ActionStatus.APPROVED, actor, note)

    def deny(self, proposal_id: str, actor: str, note: str = "") -> ActionProposal:
        return self._decide(proposal_id, ActionStatus.DENIED, actor, note)

    # -- execution outcome (ApprovalExecutor only) -----------------------------------------

    def mark_executed(
        self, proposal: ActionProposal, actor: str, result: dict[str, Any]
    ) -> ActionProposal:
        proposal.executed_at = self.clock()
        proposal.result = result
        return self._transition(proposal, ActionStatus.EXECUTED, actor, "write executed")

    def mark_failed(self, proposal: ActionProposal, actor: str, error: str) -> ActionProposal:
        proposal.executed_at = self.clock()
        proposal.error = error
        return self._transition(proposal, ActionStatus.FAILED, actor, error[:500])


# --------------------------------------------------------------------------- executor


class ApprovalExecutor:
    """The ONLY code path that calls write tools."""

    def __init__(self, service: ApprovalService, mcp: MCPRegistry) -> None:
        self.service = service
        self.mcp = mcp

    async def execute(self, proposal_id: str, actor: str) -> ActionProposal:
        proposal = self.service.get(proposal_id)
        if proposal.status is not ActionStatus.APPROVED:
            raise ApprovalError(
                f"{proposal.id} is {proposal.status.value}: only approved proposals are executed"
            )
        # Config may have changed since the proposal was made: re-check before writing.
        rejection = self.service.policy.check(proposal)
        if rejection:
            return self.service.mark_failed(proposal, actor, f"policy: {rejection}")
        try:
            async with self.mcp.write_toolset(
                proposal.capability,
                actor=f"approvals:{actor}",
                investigation_id=proposal.investigation_id,
            ) as toolset:
                outcome = await toolset.call(proposal.tool, proposal.arguments)
        except Exception as exc:  # connection errors etc. must end in FAILED, not PENDING
            return self.service.mark_failed(proposal, actor, f"{type(exc).__name__}: {exc}")
        if not outcome.ok:
            return self.service.mark_failed(
                proposal, actor, outcome.tool_call.error or "tool call failed"
            )
        data = outcome.data if isinstance(outcome.data, dict) else {"text": outcome.text}
        return self.service.mark_executed(proposal, actor, data)
