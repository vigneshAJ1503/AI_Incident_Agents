"""Shared CLI helpers."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps

import typer
from rich.console import Console

from aiops.core.config import ConfigError

console = Console()
err_console = Console(stderr=True)


def handle_errors[**P, R](func: Callable[P, R]) -> Callable[P, R]:
    """Turn expected errors into a readable message and exit code 2."""

    @wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return func(*args, **kwargs)
        except ConfigError as exc:
            err_console.print(f"[red]Configuration error:[/red] {exc}")
            raise typer.Exit(code=2) from exc

    return wrapper


EnvOption = typer.Option(
    None, "--env", "-e", help="Environment name (default: $AIOPS_ENV or 'local')."
)
