"""`aiops mcp ...` commands: inspect and call capability tools (guarded + audited)."""

from __future__ import annotations

import asyncio
import json

import typer
from rich.markup import escape
from rich.table import Table

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.config import load_settings
from aiops.mcp.client import MCPClientError
from aiops.mcp.registry import MCPRegistry

app = typer.Typer(help="Inspect and call MCP tools for a capability.", no_args_is_help=True)


@app.command("tools")
@handle_errors
def tools(capability: str, env: str | None = EnvOption) -> None:
    """List the allowlisted tools a capability exposes."""

    async def run() -> None:
        registry = MCPRegistry(load_settings(env))
        async with registry.toolset(capability, agent="cli") as toolset:
            specs = await toolset.specs()
        table = Table("Tool", "Description")
        for spec in specs:
            table.add_row(spec.name, escape(spec.description.split("\n")[0]))
        console.print(table)

    try:
        asyncio.run(run())
    except MCPClientError as exc:
        err_console.print(f"[red]MCP error:[/red] {exc}")
        raise typer.Exit(code=1) from exc


@app.command("call")
@handle_errors
def call(
    capability: str,
    tool: str,
    args: str = typer.Option("{}", "--args", "-a", help="JSON object of tool arguments."),
    env: str | None = EnvOption,
) -> None:
    """Call one tool through the guardrails (allowlist, redaction, audit)."""
    try:
        arguments = json.loads(args)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"--args is not valid JSON: {exc}") from exc

    async def run() -> None:
        registry = MCPRegistry(load_settings(env))
        async with registry.toolset(capability, agent="cli") as toolset:
            outcome = await toolset.call(tool, arguments)
        status = outcome.tool_call.status
        colour = "green" if outcome.ok else "red"
        console.print(f"[{colour}]{status}[/{colour}] {outcome.tool_call.duration_ms:.0f}ms")
        if outcome.data is not None:
            console.print_json(json.dumps(outcome.data, default=str))
        else:
            console.print(outcome.text or outcome.tool_call.error or "", markup=False)
        if not outcome.ok:
            raise typer.Exit(code=1)

    try:
        asyncio.run(run())
    except MCPClientError as exc:
        err_console.print(f"[red]MCP error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
