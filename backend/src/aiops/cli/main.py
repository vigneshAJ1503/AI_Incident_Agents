"""`aiops` command-line entry point."""

import typer

from aiops import __version__
from aiops.cli import catalog_cmd, config_cmd
from aiops.cli.common import console

app = typer.Typer(
    name="aiops",
    help="AI Incident Agents — investigate incidents from the command line.",
    no_args_is_help=True,
)
app.add_typer(config_cmd.app, name="config")
app.add_typer(catalog_cmd.app, name="catalog")


@app.callback()
def main() -> None:
    """AI Incident Agents CLI."""


@app.command()
def version() -> None:
    """Print the aiops version."""
    console.print(f"aiops {__version__}")


if __name__ == "__main__":
    app()
