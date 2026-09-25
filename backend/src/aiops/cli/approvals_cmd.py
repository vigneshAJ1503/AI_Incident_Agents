"""`aiops approvals ...`: review, approve or deny proposed write actions (UC-04).

Approving executes the action (unless --no-execute) through ApprovalExecutor, the only
code path allowed to call write tools.
"""

from __future__ import annotations

import asyncio
import getpass
import json

import typer
from rich.markup import escape
from rich.table import Table

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.config import load_settings
from aiops.core.guardrails.approvals import (
    ActionProposal,
    ActionStatus,
    ApprovalError,
    ApprovalExecutor,
    ApprovalService,
)
from aiops.mcp.registry import MCPRegistry

app = typer.Typer(help="Human approval of write actions.", no_args_is_help=True)

ByOption = typer.Option(
    None, "--by", help="Your identity for the audit log (default: the OS user)."
)


def _actor(by: str | None) -> str:
    return (by or getpass.getuser()).strip()


def _fail(exc: Exception) -> typer.Exit:
    err_console.print(f"[red]{escape(str(exc))}[/red]")
    return typer.Exit(code=1)


def print_proposal(p: ActionProposal, *, full: bool = True) -> None:
    colour = {
        "pending": "yellow",
        "approved": "cyan",
        "executed": "green",
    }.get(p.status.value, "red")
    console.print(f"[bold]{p.id}[/bold]  [{colour}]{p.status.value}[/{colour}]  {p.action}")
    console.print(
        f"tool: {p.capability}.{p.tool} · risk: {p.risk} · requested by {p.requested_by} "
        f"at {p.created_at:%Y-%m-%d %H:%M} UTC"
        + (f" · expires {p.expires_at:%Y-%m-%d %H:%M} UTC" if p.expires_at else "")
    )
    console.print(f"reason: {escape(p.reason)}")
    if p.investigation_id:
        console.print(f"investigation: {p.investigation_id}")
    if p.policy_reason:
        console.print(f"[red]policy: {escape(p.policy_reason)}[/red]")
    if p.decided_by:
        console.print(f"decided by {escape(p.decided_by)}: {escape(p.decision_note or '')}")
    if full:
        console.print("arguments:")
        console.print_json(json.dumps(p.arguments, default=str))
    if p.result is not None:
        console.print("result:")
        console.print_json(json.dumps(p.result, default=str))
    if p.error:
        console.print(f"[red]error: {escape(p.error)}[/red]")
    if full and p.history:
        table = Table("At (UTC)", "From", "To", "Actor", "Note")
        for t in p.history:
            table.add_row(
                f"{t.at:%Y-%m-%d %H:%M:%S}",
                t.from_status.value if t.from_status else "-",
                t.to_status.value,
                t.actor,
                escape(t.note),
            )
        console.print(table)


@app.command("list")
@handle_errors
def list_proposals(
    status: ActionStatus | None = typer.Option(None, "--status", "-s", help="Filter by status."),
    as_json: bool = typer.Option(False, "--json"),
    env: str | None = EnvOption,
) -> None:
    """List action proposals, newest first (expired ones are marked on the way)."""
    service = ApprovalService.from_settings(load_settings(env))
    items = service.proposals(status)
    if as_json:
        console.print_json(json.dumps([p.model_dump(mode="json") for p in items]))
        return
    table = Table(
        "Id", "Status", "Action", "Tool", "Risk", "Requested by", "Created (UTC)", "Reason"
    )
    for p in items:
        table.add_row(
            p.id,
            p.status.value,
            p.action,
            f"{p.capability}.{p.tool}",
            p.risk,
            p.requested_by,
            f"{p.created_at:%Y-%m-%d %H:%M}",
            escape(p.reason[:60]),
        )
    console.print(table)


@app.command("show")
@handle_errors
def show(proposal_id: str, env: str | None = EnvOption) -> None:
    """Show one proposal: arguments, decision, result and its full history."""
    service = ApprovalService.from_settings(load_settings(env))
    try:
        print_proposal(service.get(proposal_id))
    except ApprovalError as exc:
        raise _fail(exc) from exc


@app.command("approve")
@handle_errors
def approve(
    proposal_id: str,
    by: str | None = ByOption,
    note: str = typer.Option("", "--note", help="Why you approve (audited)."),
    execute: bool = typer.Option(
        True, "--execute/--no-execute", help="Execute right after approving (default)."
    ),
    env: str | None = EnvOption,
) -> None:
    """Approve a pending proposal and execute it."""
    settings = load_settings(env)
    service = ApprovalService.from_settings(settings)
    actor = _actor(by)
    try:
        proposal = service.approve(proposal_id, actor, note)
        if execute:
            executor = ApprovalExecutor(service, MCPRegistry(settings))
            proposal = asyncio.run(executor.execute(proposal_id, actor))
    except ApprovalError as exc:
        raise _fail(exc) from exc
    print_proposal(proposal, full=False)
    if proposal.status is ActionStatus.FAILED:
        raise typer.Exit(code=1)


@app.command("execute")
@handle_errors
def execute_cmd(proposal_id: str, by: str | None = ByOption, env: str | None = EnvOption) -> None:
    """Execute an already approved proposal (after `approve --no-execute`)."""
    settings = load_settings(env)
    service = ApprovalService.from_settings(settings)
    try:
        proposal = asyncio.run(
            ApprovalExecutor(service, MCPRegistry(settings)).execute(proposal_id, _actor(by))
        )
    except ApprovalError as exc:
        raise _fail(exc) from exc
    print_proposal(proposal, full=False)
    if proposal.status is ActionStatus.FAILED:
        raise typer.Exit(code=1)


@app.command("deny")
@handle_errors
def deny(
    proposal_id: str,
    by: str | None = ByOption,
    reason: str = typer.Option("", "--reason", help="Why you deny (audited)."),
    env: str | None = EnvOption,
) -> None:
    """Deny a pending proposal; it can never be executed."""
    service = ApprovalService.from_settings(load_settings(env))
    try:
        print_proposal(service.deny(proposal_id, _actor(by), reason), full=False)
    except ApprovalError as exc:
        raise _fail(exc) from exc
