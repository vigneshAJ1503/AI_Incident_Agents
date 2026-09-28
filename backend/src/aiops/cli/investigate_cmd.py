"""`aiops plan` and `aiops investigate`: one investigation from all agents (UC-10, UC-13)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import typer
from rich.table import Table

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.config import Settings, load_settings
from aiops.core.events import EventBus, InvestigationEvent
from aiops.core.models import Investigation
from aiops.orchestrator.engine import InvestigationRequest, Orchestrator
from aiops.orchestrator.planner import Plan
from aiops.orchestrator.replay import ReplayError, load_replay

ServiceOption = typer.Option(None, "--service", "-s", help="Service (catalog name or alias).")
EnvironmentOption = typer.Option(None, "--environment", help="Environment, e.g. production.")
SinceOption = typer.Option(None, "--since", help="Look-back window, e.g. 30m or 2h.")
StartOption = typer.Option(None, "--start", help="Window start (ISO-8601, UTC if no zone).")
EndOption = typer.Option(None, "--end", help="Window end (ISO-8601, UTC if no zone).")
JsonOption = typer.Option(False, "--json", help="Print JSON instead of tables.")


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _request(
    question: str,
    service: str | None,
    environment: str | None,
    since: str | None,
    start: datetime | None,
    end: datetime | None,
    replay: str | None,
) -> InvestigationRequest:
    return InvestigationRequest(
        question=question,
        service=service,
        environment=environment,
        since=since,
        start=_aware(start),
        end=_aware(end),
        mode="replay" if replay else "live",
    )


def build_orchestrator(
    settings: Settings, replay: str | None, bus: EventBus | None = None
) -> Orchestrator:
    try:
        source = load_replay(settings, replay) if replay else None
    except ReplayError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    return Orchestrator(settings, replay=source, bus=bus)


def print_plan(plan: Plan) -> None:
    ctx = plan.context
    parsed = plan.parsed
    console.print(
        f"[bold]service[/bold] {ctx.service or '?'} ({parsed.service_source})  "
        f"[bold]environment[/bold] {ctx.environment or '?'}  "
        f"[bold]window[/bold] {ctx.time_range.start:%Y-%m-%d %H:%M} to "
        f"{ctx.time_range.end:%H:%M} UTC ({parsed.time_source})  "
        f"[bold]symptoms[/bold] {', '.join(ctx.symptoms) or '-'}"
    )
    for note in plan.notes:
        console.print(f"[dim]note: {note}[/dim]")
    if plan.needs_clarification:
        console.print(f"[yellow]Clarification needed:[/yellow] {plan.clarification_question}")
        return
    table = Table("Round", "Agent", "Objective", "Depends on")
    for step in plan.steps:
        table.add_row(
            str(step.round), step.agent, step.objective, f"{len(step.depends_on)} step(s)"
        )
    console.print(table)


@handle_errors
def plan(
    question: str = typer.Argument(..., help="The engineer's question."),
    service: str | None = ServiceOption,
    environment: str | None = EnvironmentOption,
    since: str | None = SinceOption,
    start: datetime | None = StartOption,
    end: datetime | None = EndOption,
    as_json: bool = JsonOption,
    env: str | None = EnvOption,
) -> None:
    """Show the investigation plan for a question (no agent runs, no tokens)."""
    settings = load_settings(env)
    orchestrator = build_orchestrator(settings, None)
    request = _request(question, service, environment, since, start, end, None)
    result = asyncio.run(orchestrator.plan(request))
    if as_json:
        console.print_json(result.model_dump_json())
    else:
        print_plan(result)


def print_event(event: InvestigationEvent) -> None:
    data = event.data
    agent = f"{event.agent} " if event.agent else ""
    detail = {
        "round_started": lambda: f"round {data.get('round')}: {', '.join(data.get('agents', []))}",
        "agent_started": lambda: str(data.get("objective", ""))[:80],
        "tool_called": lambda: (
            f"{data.get('tool')} {data.get('status')} {data.get('duration_ms')}ms"
        ),
        "agent_finished": lambda: (
            f"{data.get('status')} signals={','.join(data.get('signals', []))} "
            f"evidence={data.get('evidence_count')} tokens={data.get('tokens')}"
        ),
        "error": lambda: str(data.get("message")),
        "clarification_needed": lambda: str(data.get("question")),
        "investigation_finished": lambda: f"{data.get('status')} in {data.get('duration_ms')}ms",
    }.get(event.type, lambda: "")()
    if event.type != "evidence_added":
        console.print(f"[dim]{event.seq:>3} {event.type:<22}[/dim] {agent}{detail}")


def print_investigation(inv: Investigation) -> None:
    colour = {"completed": "green", "partial": "yellow"}.get(inv.status.value, "red")
    console.print(
        f"\n[bold {colour}]{inv.id}: {inv.status.value}[/bold {colour}] "
        f"({inv.mode}, {inv.duration_ms or 0:.0f} ms, tokens {inv.usage.total_tokens})"
    )
    if inv.clarification_question:
        console.print(f"[yellow]{inv.clarification_question}[/yellow]")
    table = Table("Round", "Agent", "Status", "Signals", "Evidence")
    by_step = {r.task_id: r for r in inv.results}
    for step in inv.steps:
        result = by_step.get(step.id)
        table.add_row(
            str(step.round),
            step.agent,
            result.status.value if result else step.status.value,
            ", ".join(result.signals) if result else "",
            str(len(result.evidence)) if result else "",
        )
    console.print(table)
    report = inv.report
    if report is not None:
        console.print(report.markdown or report.summary)


def _save(settings: Settings, inv: Investigation, bus: EventBus) -> None:
    """Persist to the evidence store; a store outage never loses the printed result."""
    from aiops.store.db import StoreError
    from aiops.store.repository import InvestigationStore

    store = InvestigationStore.from_settings(settings)

    async def _persist() -> None:
        try:
            await store.save(inv)
            await store.save_events(bus.history(inv.id))
        finally:
            await store.close()

    try:
        store.migrate()
        asyncio.run(_persist())
    except (StoreError, OSError) as exc:
        err_console.print(f"[yellow]Not saved to the evidence store:[/yellow] {exc}")
        return
    err_console.print(f"[dim]saved: aiops show {inv.id}[/dim]")


@handle_errors
def investigate(
    question: str = typer.Argument("", help="The engineer's question (optional with --replay)."),
    service: str | None = ServiceOption,
    environment: str | None = EnvironmentOption,
    since: str | None = SinceOption,
    start: datetime | None = StartOption,
    end: datetime | None = EndOption,
    replay: str | None = typer.Option(
        None, "--replay", help="Replay a scenario's recorded fixtures (S0-S5): zero tokens."
    ),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Don't stream events."),
    save: bool = typer.Option(
        True, "--save/--no-save", help="Store it in the evidence store (aiops history/show)."
    ),
    as_json: bool = JsonOption,
    env: str | None = EnvOption,
) -> None:
    """Run a full investigation: plan -> round 1 -> gap analysis -> round 2 -> report."""
    if not question and not replay:
        err_console.print("[red]Give a question, or --replay <scenario>.[/red]")
        raise typer.Exit(code=2)
    settings = load_settings(env)
    bus = EventBus()
    if not quiet and not as_json:
        bus.subscribe(print_event)
    orchestrator = build_orchestrator(settings, replay, bus)
    request = _request(question, service, environment, since, start, end, replay)
    inv = asyncio.run(orchestrator.investigate(request))
    if save:
        _save(settings, inv, bus)
    if as_json:
        console.print_json(json.dumps(inv.model_dump(mode="json")))
    else:
        print_investigation(inv)
    if inv.status.value == "failed":
        raise typer.Exit(code=1)
