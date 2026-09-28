"""`aiops evaluate`: the full-system evaluation (PR-040, docs/evals.md)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer
from rich.table import Table

from aiops.cli.common import EnvOption, console, err_console, handle_errors
from aiops.core.config import load_settings
from aiops.evals.runner import MODES, EvalError, EvalPaths, Mode
from aiops.evals.system import (
    BASELINES_DIR,
    Regression,
    SystemReport,
    compare,
    load_baseline,
    run_system_eval,
    write_baseline,
    write_report,
)


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0%}"


def print_summary(report: SystemReport) -> None:
    info = report.info
    console.print(
        f"[bold]{info.mode}[/bold] · profile [bold]{info.profile}[/bold] · LLM {info.llm_used}"
        f" · git {info.git_sha[:12]}{' (dirty)' if info.git_dirty else ''} · judge {info.judge}"
    )
    if report.agents:
        table = Table(title="Agents")
        for column in ("Agent", "Pass", "FP rate", "Citations", "Tools", "Tokens", "p50", "Cost"):
            table.add_column(column)
        for a in report.agents:
            table.add_row(
                a.agent,
                f"{a.passed}/{a.scenarios}",
                _pct(a.false_positive_rate),
                _pct(a.citation_validity),
                f"{a.avg_tool_calls:g}",
                f"{a.avg_tokens:g}",
                f"{a.p50_latency_ms:.0f} ms",
                f"${a.cost_usd:.4f}",
            )
        console.print(table)
    planner = report.planner_summary
    if planner:
        console.print(
            f"planner: service accuracy {_pct(planner.service_accuracy)} · clarification "
            f"{_pct(planner.clarification_accuracy)} · invented services "
            f"{planner.invented_services} · {planner.passed}/{planner.cases} cases pass"
        )
    inv = report.investigation_summary
    if inv:
        table = Table(title="Investigations")
        for column in ("Scenario", "Result", "Service", "Root cause", "Conf.", "Severity", "Time"):
            table.add_column(column)
        for r in report.investigations:
            table.add_row(
                r.scenario,
                "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]",
                r.service or "-",
                "correct" if r.root_cause_correct else "[red]wrong[/red]",
                f"{r.confidence:.2f}",
                r.severity,
                f"{r.duration_ms:.0f} ms",
            )
        console.print(table)
        console.print(
            f"root cause {inv.root_cause_correct}/{inv.incidents} ({_pct(inv.root_cause_accuracy)})"
            f" · judge {_pct(inv.judge_accuracy)} · false positives {inv.false_positives}/"
            f"{inv.healthy_scenarios} · Brier {inv.brier_score:.3f} · citations "
            f"{_pct(inv.citation_validity)} · p50 {inv.p50_time_to_report_ms:.0f} ms · tokens "
            f"{inv.total_tokens} · cost ${inv.total_cost_usd:.4f}"
        )
    for note in report.notes:
        console.print(f"[yellow]note:[/yellow] {note}")


def _agents(value: str | None) -> str | list[str] | None:
    if value is None:
        return None
    names = [n.strip() for n in value.split(",") if n.strip()]
    if not names or names == ["none"]:
        return []
    return "all" if names == ["all"] else names


@handle_errors
def evaluate(
    agents: str | None = typer.Option(
        None,
        "--agents",
        "-a",
        help="all | comma-separated agent names | none. Default: all (or none with "
        "--investigations alone).",
    ),
    investigations: bool | None = typer.Option(
        None,
        "--investigations/--no-investigations",
        help="End-to-end investigations + planner cases. Default: on (off with --agents alone).",
    ),
    mode: str = typer.Option(
        "replay", "--mode", "-m", help="replay (fixtures, zero tokens) | live (seed + real LLM)."
    ),
    scenario: list[str] = typer.Option(
        [], "--scenario", "-S", help="Scenario id (repeatable). Default: all. Disables the gate."
    ),
    judge: bool | None = typer.Option(
        None,
        "--judge/--no-judge",
        help="Also score root causes with an LLM judge (needs a key; default evals.llm_judge).",
    ),
    baseline: Path | None = typer.Option(
        None, "--baseline", help="Baseline JSON. Default: <repo>/evals/baselines/<mode>.json."
    ),
    gate: bool = typer.Option(
        True, "--gate/--no-gate", help="Exit 1 when a metric is worse than the baseline."
    ),
    update_baseline: bool = typer.Option(
        False, "--update-baseline", help="Write this run's numbers as the new baseline."
    ),
    out: Path | None = typer.Option(
        None, "--out", "-o", help="Report directory. Default: <repo>/evals/reports/."
    ),
    write: bool = typer.Option(True, "--write/--no-write", help="Write the report files."),
    env: str | None = EnvOption,
) -> None:
    """Evaluate the whole system (agents + planner + investigations) into ONE scorecard."""
    if mode not in MODES:
        raise typer.BadParameter(f"mode must be one of {', '.join(MODES)}", param_hint="--mode")
    run_mode: Mode = "live" if mode == "live" else "replay"
    selection = _agents(agents)
    if selection is None:
        selection = [] if investigations else "all"
    if investigations is None:
        investigations = agents is None
    if not selection and not investigations:
        raise typer.BadParameter("nothing to evaluate", param_hint="--agents/--investigations")

    settings = load_settings(env)
    if run_mode == "replay":
        # Replay evals must be deterministic and zero-token, even when the developer's .env
        # enables AIOPS_REPLAY_LLM=real for the demo (a rate-limited real LLM would otherwise
        # make the regression gate flaky). After load_settings: it loads .env.
        os.environ.pop("AIOPS_REPLAY_LLM", None)
    paths = EvalPaths.discover(settings.config_dir)
    try:
        report = asyncio.run(
            run_system_eval(
                settings,
                agents=selection,
                investigations=investigations,
                mode=run_mode,
                scenario_ids=scenario,
                judge=judge,
                paths=paths,
                progress=lambda msg: console.print(f"[dim]{msg}[/dim]"),
            )
        )
    except EvalError as exc:
        err_console.print(f"[red]Evaluation failed:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    print_summary(report)
    baseline_path = baseline or paths.repo_root / "evals" / BASELINES_DIR / f"{run_mode}.json"
    regressions: list[Regression] | None = None
    reference = load_baseline(baseline_path)
    if scenario:
        console.print("[dim]gate skipped: a scenario subset isn't comparable to the baseline[/dim]")
    elif reference is None:
        console.print(f"[yellow]no baseline at {baseline_path}; gate skipped[/yellow]")
    elif reference.mode != run_mode:
        console.print(f"[yellow]baseline is for {reference.mode}, not {run_mode}; gate skipped")
    else:
        regressions = compare(report, reference, settings.evals.tolerance)

    if write:
        markdown, json_path = write_report(report, out or paths.reports, regressions)
        console.print(f"[dim]wrote {markdown} and {json_path.name}[/dim]")
    if update_baseline:
        if scenario:
            raise typer.BadParameter(
                "a baseline must cover every scenario", param_hint="--update-baseline"
            )
        console.print(f"[dim]baseline written: {write_baseline(report, baseline_path)}[/dim]")
        return
    if regressions:
        err_console.print("[red]Regression gate failed:[/red]")
        for regression in regressions:
            err_console.print(f"  - {regression}")
        if gate:
            raise typer.Exit(code=1)
    elif regressions is not None:
        console.print("[green]Regression gate: no metric is worse than the baseline.[/green]")
