"""`aiops tickets draft`: turn findings into a ticket PROPOSAL (UC-04). Nothing is written
until a human runs `aiops approvals approve <id>`.
"""

from __future__ import annotations

import getpass
import json
from pathlib import Path

import typer

from aiops.agents.tickets_agent.draft import ACTIONS, draft_ticket, load_results
from aiops.cli.approvals_cmd import print_proposal
from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import load_settings
from aiops.core.guardrails.approvals import ActionStatus, ApprovalService, Risk
from aiops.mcp import tickets

app = typer.Typer(help="Ticket actions (drafts go through human approval).", no_args_is_help=True)

INVESTIGATIONS_DIR = ".data/investigations"


@app.command("draft")
@handle_errors
def draft(
    investigation: str | None = typer.Option(
        None,
        "--investigation",
        "-i",
        help=f"Investigation id; reads {INVESTIGATIONS_DIR}/<id>.json.",
    ),
    from_result: Path | None = typer.Option(
        None,
        "--from-result",
        "-f",
        help="AgentResult / list of AgentResults / Investigation JSON (e.g. `aiops agent run ... --json`).",
    ),
    service: str | None = typer.Option(
        None, "--service", "-s", help="Service (labels/components from the catalog)."
    ),
    comment_on: str | None = typer.Option(
        None, "--comment-on", help="Add the findings as a comment on this ticket instead."
    ),
    issue_type: str = typer.Option("Bug", "--issue-type"),
    requested_by: str | None = typer.Option(None, "--requested-by", help="Default: OS user."),
    env: str | None = EnvOption,
) -> None:
    """Draft a ticket (title, description with findings + evidence links, labels) as a proposal."""
    if (investigation is None) == (from_result is None):
        raise typer.BadParameter("pass exactly one of --investigation or --from-result")
    settings = load_settings(env)
    path = from_result or settings.repo_path(f"{INVESTIGATIONS_DIR}/{investigation}.json")
    if not path.is_file():
        err_console.print(f"[red]Not found:[/red] {path}")
        raise typer.Exit(code=1)
    try:
        results, inv_id, inv_service = load_results(json.loads(path.read_text()))
    except ValueError as exc:  # includes JSON and validation errors
        err_console.print(f"[red]Can't read findings from {path}:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    catalog = ServiceCatalog.from_settings(settings)
    name = service or inv_service
    labels: list[str] = []
    components: list[str] = []
    if name:
        resolution = catalog.resolve(name)
        if resolution.service is None:
            err_console.print(f"[red]Unknown service[/red] '{name}'")
            raise typer.Exit(code=1)
        name = resolution.service.name
        ids = resolution.service.identifiers("tickets")
        labels = [str(v) for v in ids.get("labels", [])]
        components = [str(v) for v in ids.get("components", [])]

    ticket = draft_ticket(
        results,
        service=name,
        labels=labels,
        components=components,
        investigation_id=inv_id or investigation,
        issue_type=issue_type,
    )
    cap = settings.capability("tickets")
    project = str(cap.settings.get("project_key", "OPS"))
    risk: Risk
    if comment_on:
        tool, arguments = tickets.ADD_COMMENT, ticket.comment_arguments(comment_on)
        reason = f"Add investigation findings to {comment_on}"
        risk = "low"
    else:
        tool, arguments = tickets.CREATE_ISSUE, ticket.create_arguments(project)
        reason = f"Create a {issue_type} in {project} for: {ticket.summary}"
        risk = "medium"
    proposal = ApprovalService.from_settings(settings).propose(
        action=ACTIONS[tool],
        capability="tickets",
        tool=tool,
        arguments=arguments,
        reason=reason,
        requested_by=requested_by or getpass.getuser(),
        risk=risk,
        investigation_id=ticket.investigation_id,
    )
    console.print(ticket.summary, style="bold", markup=False)
    console.print()
    console.print(ticket.description, markup=False)
    console.print()
    print_proposal(proposal, full=False)
    if proposal.status is ActionStatus.REJECTED:
        raise typer.Exit(code=1)
    console.print(
        f"\nNothing has been written. Review, then: [bold]aiops approvals approve {proposal.id}[/bold]"
        f" (or deny {proposal.id} --reason ...)"
    )
