"""`aiops agent ...` commands: run any registered agent standalone."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.table import Table

import aiops.agents  # noqa: F401  (registers built-in agents)
from aiops.agents.deps import build_deps
from aiops.agents.registry import AGENTS
from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.config import load_settings
from aiops.core.events import CallbackEventSink, Event
from aiops.core.models import AgentResult, AgentTask, IncidentContext, TimeRange

app = typer.Typer(help="Run specialized agents standalone.", no_args_is_help=True)


@app.command("list")
def list_agents() -> None:
    """List registered agents."""
    table = Table("Agent", "Version", "Capabilities", "Description")
    for spec in AGENTS.specs():
        table.add_row(spec.name, spec.version, ", ".join(spec.capabilities), spec.description)
    console.print(table)


def _print_event(event: Event) -> None:
    detail = " ".join(f"{k}={v}" for k, v in event.data.items() if k != "summary")
    console.print(f"[dim]{event.timestamp:%H:%M:%S} {event.agent} {event.type} {detail}[/dim]")


def print_result(result: AgentResult) -> None:
    colour = {"success": "green", "no_signal": "cyan", "partial": "yellow"}.get(
        result.status.value, "red"
    )
    console.print(
        f"\n[bold {colour}]{result.agent}: {result.status.value}[/bold {colour}]  {result.summary}"
    )
    if result.findings:
        table = Table("Kind", "Type", "Description", "Evidence", "Conf.")
        for f in result.findings:
            conf = f"{f.confidence:.2f}" if f.confidence is not None else ""
            table.add_row(f.kind.value, f.type, f.description, ", ".join(f.evidence_ids), conf)
        console.print(table)
    if result.evidence:
        table = Table("Evidence id", "Source", "Summary", "Link")
        for e in result.evidence:
            table.add_row(e.id, e.source, e.summary, e.link or "")
        console.print(table)
    if result.signals:
        console.print(f"signals: {', '.join(result.signals)}")
    console.print(
        f"[dim]tool calls={len(result.tool_calls)} tokens={result.usage.total_tokens} "
        f"llm calls={result.usage.calls} model={result.model} prompt={result.prompt_version} "
        f"duration={result.duration_ms:.0f}ms[/dim]"
    )


@app.command("run")
@handle_errors
def run(
    name: str = typer.Argument(..., help="Agent name, e.g. logs."),
    question: str = typer.Argument(..., help="The engineer's question."),
    service: str = typer.Option(..., "--service", "-s", help="Service name or alias."),
    environment: str = typer.Option("production", "--environment", "-E"),
    since: str = typer.Option("30m", "--since", help="Look-back window, e.g. 30m, 2h."),
    end: datetime | None = typer.Option(
        None, "--end", help="Window end, UTC (e.g. 2026-09-25T10:30:00). Default: now."
    ),
    record: Path | None = typer.Option(
        None, "--record", help="Save MCP responses as fixtures here."
    ),
    replay: Path | None = typer.Option(
        None, "--replay", help="Serve MCP responses from fixtures here."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the AgentResult as JSON."),
    env: str | None = EnvOption,
) -> None:
    """Run one agent on one question (no orchestrator)."""
    settings = load_settings(env)
    sink = CallbackEventSink(_print_event) if not as_json else None
    deps = build_deps(settings, events=sink, record_dir=record, replay_dir=replay)
    resolution = deps.catalog.resolve(service)
    if resolution.service is None:
        err_console.print(f"[red]Unknown service[/red] '{service}' {resolution.candidates or ''}")
        raise typer.Exit(code=1)
    env_name = deps.catalog.resolve_environment(environment)
    if env_name is None:
        err_console.print(f"[red]Unknown environment[/red] '{environment}'")
        raise typer.Exit(code=1)
    window_end = (end.replace(tzinfo=UTC) if end.tzinfo is None else end) if end else None
    context = IncidentContext(
        question=question,
        service=resolution.service.name,
        environment=env_name,
        time_range=TimeRange.last(since, now=window_end),
    )
    agent = AGENTS.get(name)(deps)
    task = AgentTask(agent=name, objective=question, context=context)
    result = asyncio.run(agent.run(task))
    if as_json:
        console.print_json(result.model_dump_json())
    else:
        print_result(result)
    if result.status.value == "failed":
        raise typer.Exit(code=1)
