"""`aiops llm ...` commands."""

from __future__ import annotations

import asyncio
import time

import typer

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.config import ModelRole, load_settings
from aiops.llm.base import ChatMessage, LLMError
from aiops.llm.factory import create_provider

app = typer.Typer(help="LLM provider utilities (hosted free tiers only).", no_args_is_help=True)


@app.command("ping")
@handle_errors
def ping(
    role: str = typer.Option("fast", help="Model role to test: fast | agent | rca."),
    env: str | None = EnvOption,
) -> None:
    """Send a tiny request to the configured provider and report latency/tokens."""
    if role not in ("fast", "agent", "rca"):
        raise typer.BadParameter("role must be fast, agent or rca")
    settings = load_settings(env)
    provider = create_provider(settings.llm)
    model_role: ModelRole = role  # type: ignore[assignment]
    started = time.perf_counter()
    try:
        response = asyncio.run(
            provider.generate(
                [ChatMessage.user("Reply with exactly one word: pong")],
                role=model_role,
                max_tokens=10,
            )
        )
    except LLMError as exc:
        err_console.print(f"[red]LLM error:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    elapsed_ms = (time.perf_counter() - started) * 1000
    console.print(
        f"[green]ok[/green] provider={provider.name} model={response.model} "
        f"reply={response.content!r} latency={elapsed_ms:.0f}ms tokens={response.usage.total_tokens}"
    )
