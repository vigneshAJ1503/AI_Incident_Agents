"""`aiops eval ...` commands: score agents against scenario ground truth."""

from __future__ import annotations

import asyncio
from pathlib import Path

import typer
from rich.table import Table

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.config import load_settings
from aiops.evals.runner import (
    DEFAULT_MIN_PASS_RATE,
    MODES,
    EvalError,
    EvalPaths,
    EvalReport,
    Mode,
    ScenarioEval,
    run_eval,
    write_report,
)

app = typer.Typer(help="Evaluate agents against scenario ground truth.", no_args_is_help=True)


def _progress(result: ScenarioEval) -> None:
    verdict = "[green]PASS[/green]" if result.passed else "[red]FAIL[/red]"
    console.print(f"[dim]{result.scenario}[/dim] {verdict} {result.status}")


def print_report(report: EvalReport) -> None:
    table = Table(title=f"{report.agent} agent · {report.mode} eval")
    for column in ("Scenario", "Result", "Status", "Signals", "Failed checks"):
        table.add_column(column)
    for column in ("Tools", "LLM", "Tokens", "Latency"):
        table.add_column(column, justify="right")
    for r in report.results:
        result = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
        if r.false_positive:
            result += " [red](FP)[/red]"
        table.add_row(
            r.scenario,
            result,
            r.status,
            ", ".join(r.signals) or "-",
            "\n".join(f"{c.name}: {c.detail}" for c in r.failed_checks) or "-",
            str(r.tool_calls),
            str(r.llm_calls),
            str(r.tokens),
            f"{r.latency_ms:.0f} ms",
        )
    console.print(table)
    s = report.summary
    fp = (
        f"{s.false_positives}/{s.healthy_scenarios}" if s.false_positive_rate is not None else "n/a"
    )
    console.print(
        f"pass rate [bold]{s.passed}/{s.scenarios} ({s.pass_rate:.0%})[/bold] · "
        f"false positives {fp} · valid citations {s.valid_findings}/{s.findings} · "
        f"avg tool calls {s.avg_tool_calls:g} · avg tokens {s.avg_tokens:g} · "
        f"latency avg {s.avg_latency_ms:.0f} ms / p50 {s.p50_latency_ms:.0f} ms"
    )


@app.command("run")
@handle_errors
def run(
    agent: str = typer.Option("logs", "--agent", "-a", help="Registered agent name."),
    mode: str = typer.Option(
        "replay", "--mode", "-m", help="replay (fixtures, zero tokens) | live (seed + real LLM)."
    ),
    scenario: list[str] = typer.Option(
        [], "--scenario", "-S", help="Scenario id (repeatable). Default: all for the agent."
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Report directory. Default: <repo>/evals/reports/."
    ),
    min_pass_rate: float | None = typer.Option(
        None,
        "--min-pass-rate",
        min=0.0,
        max=1.0,
        help="Exit 1 below this pass rate. Default: 1.0 replay, 0.8 live.",
    ),
    write: bool = typer.Option(True, "--write/--no-write", help="Write the report files."),
    env: str | None = EnvOption,
) -> None:
    """Run an agent on scenarios, print a scorecard and write Markdown + JSON reports."""
    if mode not in MODES:
        raise typer.BadParameter(f"mode must be one of {', '.join(MODES)}", param_hint="--mode")
    eval_mode: Mode = "live" if mode == "live" else "replay"
    settings = load_settings(env)
    paths = EvalPaths.discover(settings.config_dir)
    try:
        report = asyncio.run(
            run_eval(
                settings,
                agent,
                mode=eval_mode,
                scenario_ids=scenario,
                paths=paths,
                on_result=_progress,
            )
        )
    except EvalError as exc:
        err_console.print(f"[red]Eval failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    print_report(report)
    if write:
        markdown, json_path = write_report(report, out or paths.reports)
        console.print(f"[dim]wrote {markdown} and {json_path.name}[/dim]")
    if eval_mode == "live":
        console.print(
            "[dim]Elasticsearch now holds the last scenario: `make seed-logs S=S1`.[/dim]"
        )

    threshold = DEFAULT_MIN_PASS_RATE[eval_mode] if min_pass_rate is None else min_pass_rate
    if report.summary.pass_rate < threshold:
        err_console.print(
            f"[red]Pass rate {report.summary.pass_rate:.0%} is below {threshold:.0%}.[/red]"
        )
        raise typer.Exit(code=1)
