"""`aiops` command-line entry point."""

import typer
from rich.console import Console

from aiops import __version__

app = typer.Typer(
    name="aiops",
    help="AI Incident Agents — investigate incidents from the command line.",
    no_args_is_help=True,
)
console = Console()


@app.callback()
def main() -> None:
    """AI Incident Agents CLI."""


@app.command()
def version() -> None:
    """Print the aiops version."""
    console.print(f"aiops {__version__}")


if __name__ == "__main__":
    app()
