"""`aiops` command-line entry point."""

import typer

from aiops import __version__
from aiops.cli import (
    agent_cmd,
    approvals_cmd,
    catalog_cmd,
    config_cmd,
    eval_cmd,
    knowledge_cmd,
    llm_cmd,
    mcp_cmd,
    prompts_cmd,
    schemas_cmd,
    seed_cmd,
    tickets_cmd,
)
from aiops.cli.common import console

app = typer.Typer(
    name="aiops",
    help="AI Incident Agents — investigate incidents from the command line.",
    no_args_is_help=True,
)
app.add_typer(config_cmd.app, name="config")
app.add_typer(catalog_cmd.app, name="catalog")
app.add_typer(schemas_cmd.app, name="schemas")
app.add_typer(llm_cmd.app, name="llm")
app.add_typer(prompts_cmd.app, name="prompts")
app.add_typer(mcp_cmd.app, name="mcp")
app.add_typer(agent_cmd.app, name="agent")
app.add_typer(seed_cmd.app, name="seed")
app.add_typer(eval_cmd.app, name="eval")
app.add_typer(knowledge_cmd.app, name="knowledge")
app.add_typer(approvals_cmd.app, name="approvals")
app.add_typer(tickets_cmd.app, name="tickets")


@app.callback()
def main() -> None:
    """AI Incident Agents CLI."""


@app.command()
def version() -> None:
    """Print the aiops version."""
    console.print(f"aiops {__version__}")


if __name__ == "__main__":
    app()
