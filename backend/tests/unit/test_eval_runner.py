"""Eval runner: selection, metrics, reports and CLI (replay fixtures, zero tokens)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiops.cli.main import app
from aiops.core.config import load_settings
from aiops.core.models import (
    AgentResult,
    AgentStatus,
    ClaimKind,
    Evidence,
    EvidenceKind,
    Finding,
    TimeRange,
)
from aiops.evals.replay import ReplayMeta
from aiops.evals.runner import (
    EvalError,
    EvalPaths,
    EvalReport,
    EvalSummary,
    ScenarioEval,
    citation_counts,
    evaluate_result,
    render_markdown,
    run_eval,
    select_scenarios,
    write_report,
)
from aiops.evals.scenario import load_scenarios
from aiops.evals.scoring import Check
from tests.conftest import REPO_ROOT

cli = CliRunner()
SCENARIOS = load_scenarios(REPO_ROOT / "scenarios")
BY_ID = {s.id: s for s in SCENARIOS}


def _result(status: AgentStatus, signals: list[str], findings: list[Finding] | None = None):  # type: ignore[no-untyped-def]
    evidence = Evidence(id="ev-000000000001", kind=EvidenceKind.LOG, source="logs.x", summary="x")
    return AgentResult(
        agent="logs",
        task_id="t",
        status=status,
        summary="Database connection timeouts",
        signals=signals,
        evidence=[evidence],
        findings=findings or [],
    )


def _eval(scenario: str, passed: bool, *, healthy: bool = False, **kw: object) -> ScenarioEval:
    return ScenarioEval(
        scenario=scenario,
        title=f"title {scenario}",
        healthy=healthy,
        status=str(kw.pop("status", "success")),
        checks=[Check(name="status", passed=passed, detail="d")],
        **kw,  # type: ignore[arg-type]
    )


# --------------------------------------------------------------------------- selection


def test_select_defaults_to_scenarios_with_agent_expectations() -> None:
    assert [s.id for s in select_scenarios(SCENARIOS, "logs", [])] == [
        "S0",
        "S1",
        "S2",
        "S3",
        "S4",
        "S5",
    ]
    assert [s.id for s in select_scenarios(SCENARIOS, "logs", ["s3", "S1", "S3"])] == ["S1", "S3"]


def test_select_rejects_unknown_or_uncovered() -> None:
    with pytest.raises(EvalError, match="Unknown scenario 'S99'"):
        select_scenarios(SCENARIOS, "logs", ["S99"])
    with pytest.raises(EvalError, match=r"has no agents/jira\.yaml"):
        select_scenarios(SCENARIOS, "jira", ["S1"])
    with pytest.raises(EvalError, match="No scenario defines expectations"):
        select_scenarios(SCENARIOS, "jira", [])


# --------------------------------------------------------------------------- metrics


def test_false_positive_on_healthy_scenario() -> None:
    s0 = BY_ID["S0"]
    clean = evaluate_result(s0, "logs", _result(AgentStatus.NO_SIGNAL, ["no_errors"]))
    assert clean.healthy and not clean.false_positive and clean.passed
    noisy = evaluate_result(s0, "logs", _result(AgentStatus.NO_SIGNAL, ["error_rate_up"]))
    assert noisy.false_positive and not noisy.passed
    claims = evaluate_result(s0, "logs", _result(AgentStatus.SUCCESS, []))
    assert claims.false_positive
    incident = evaluate_result(BY_ID["S1"], "logs", _result(AgentStatus.SUCCESS, ["x"]))
    assert not incident.healthy and not incident.false_positive


def test_citation_counts() -> None:
    cited = Finding(
        kind=ClaimKind.FACT, type="t", description="d", evidence_ids=["ev-000000000001"]
    )
    hypothesis = Finding(kind=ClaimKind.HYPOTHESIS, type="t", description="d")
    result = _result(AgentStatus.SUCCESS, [], [cited, hypothesis])
    assert citation_counts(result) == (2, 2)
    dangling = cited.model_copy(update={"evidence_ids": ["ev-ffffffffffff"]})
    bypass = result.model_copy(update={"findings": [cited, dangling]})  # skips validation
    assert citation_counts(bypass) == (2, 1)
    check = next(
        c
        for c in evaluate_result(BY_ID["S1"], "logs", bypass).checks
        if c.name == "evidence_citations"
    )
    assert not check.passed and check.detail == "1/2 findings cite existing evidence"


def test_summary_aggregates() -> None:
    results = [
        _eval("S0", True, healthy=True, status="no_signal", tool_calls=3, latency_ms=10),
        _eval(
            "S1",
            True,
            tool_calls=4,
            tokens=100,
            llm_calls=2,
            latency_ms=30,
            findings=2,
            valid_findings=2,
        ),
        _eval(
            "S2",
            False,
            tool_calls=5,
            tokens=200,
            llm_calls=1,
            latency_ms=20,
            findings=2,
            valid_findings=1,
        ),
    ]
    s = EvalSummary.of(results)
    assert (s.scenarios, s.passed) == (3, 2)
    assert s.pass_rate == 0.6667
    assert (s.healthy_scenarios, s.false_positives, s.false_positive_rate) == (1, 0, 0.0)
    assert s.citation_validity == 0.75
    assert s.avg_tool_calls == 4.0 and s.avg_tokens == 100.0 and s.total_tokens == 300
    assert s.avg_latency_ms == 20.0 and s.p50_latency_ms == 20.0
    assert EvalSummary.of([_eval("S1", True)]).false_positive_rate is None


# --------------------------------------------------------------------------- running


def _settings():  # type: ignore[no-untyped-def]
    return load_settings("local", REPO_ROOT / "config")


def test_replay_single_scenario() -> None:
    report = asyncio.run(run_eval(_settings(), "logs", scenario_ids=["S1"]))
    (result,) = report.results
    assert result.passed and result.status == "success"
    assert "db_timeout_errors_up" in result.signals
    assert result.tool_calls == 4 and result.llm_calls == 1 and result.tokens == 0
    assert report.model == "fake" and (report.prompt_version or "").startswith("logs/v2@")


def test_replay_missing_fixtures_fails_the_scenario(tmp_path: Path) -> None:
    paths = replace(EvalPaths.discover(REPO_ROOT / "config"), fixtures=tmp_path)
    report = asyncio.run(run_eval(_settings(), "logs", scenario_ids=["S0"], paths=paths))
    (result,) = report.results
    assert not result.passed and result.status == "error"
    assert "No recorded fixtures" in (result.error or "")
    assert report.summary.pass_rate == 0.0


def test_live_mode_requires_a_hosted_llm_key() -> None:
    with pytest.raises(EvalError, match="OPENAI_COMPAT_API_KEY"):
        asyncio.run(run_eval(_settings(), "logs", mode="live"))


def test_unknown_mode_rejected() -> None:
    with pytest.raises(EvalError, match="Unknown mode"):
        asyncio.run(run_eval(_settings(), "logs", mode="dry"))  # type: ignore[arg-type]


# --------------------------------------------------------------------------- reports


def _report(results: list[ScenarioEval]) -> EvalReport:
    return EvalReport(
        agent="logs",
        mode="replay",
        environment="local",
        generated_at=datetime(2026, 9, 26, 8, 0, tzinfo=UTC),
        anchor="2026-09-25T10:30:00+00:00",
        results=results,
        summary=EvalSummary.of(results),
    )


def test_markdown_and_json_reports(tmp_path: Path) -> None:
    report = _report(
        [
            _eval("S0", False, healthy=True, status="success", signals=["error_rate_up"]),
            _eval("S1", True, signals=["db_timeout_errors_up"], latency_ms=12.3),
        ]
    )
    markdown = render_markdown(report)
    assert markdown.startswith("# Eval scorecard: `logs` agent (replay)")
    assert "| Pass rate | 1/2 (50%) |" in markdown
    assert "| False-positive rate (healthy scenarios) | 1/1 (100%) |" in markdown
    assert "| S0 title S0 | FAIL (false positive) | success | error_rate_up |" in markdown
    assert "- [ ] `status`: d" in markdown and "make eval AGENT=logs MODE=replay" in markdown

    md_path, json_path = write_report(report, tmp_path / "reports")
    assert md_path.name == "2026-09-26-logs-replay.md"
    assert json_path.name == "2026-09-26-logs-replay.json"
    payload = json.loads(json_path.read_text())
    assert [r["passed"] for r in payload["results"]] == [False, True]
    assert payload["results"][0]["false_positive"] is True
    assert payload["summary"]["pass_rate"] == 0.5


# --------------------------------------------------------------------------- CLI


def test_cli_replay_writes_reports(tmp_path: Path) -> None:
    result = cli.invoke(
        app, ["eval", "run", "--agent", "logs", "-S", "S0", "-S", "S2", "--out", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "pass rate 2/2 (100%)" in result.stdout
    files = sorted(p.suffix for p in tmp_path.iterdir())
    assert files == [".json", ".md"]


def test_cli_live_without_key_exits_2() -> None:
    result = cli.invoke(app, ["eval", "run", "--mode", "live", "--no-write"])
    assert result.exit_code == 2
    assert "OPENAI_COMPAT_API_KEY" in result.output


def test_cli_below_min_pass_rate_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_eval(*args: object, **kwargs: object) -> EvalReport:
        return _report([_eval("S1", True), _eval("S2", False)])

    monkeypatch.setattr("aiops.cli.eval_cmd.run_eval", fake_run_eval)
    failing = cli.invoke(app, ["eval", "run", "--no-write"])
    assert failing.exit_code == 1
    assert "below 100%" in failing.output
    lenient = cli.invoke(app, ["eval", "run", "--no-write", "--min-pass-rate", "0.5"])
    assert lenient.exit_code == 0, lenient.output


def test_cli_rejects_unknown_mode() -> None:
    result = cli.invoke(app, ["eval", "run", "--mode", "dry", "--no-write"])
    assert result.exit_code == 2


def test_replay_meta_sets_the_task_window(tmp_path: Path) -> None:
    """Fixtures recorded from a live fault carry their window in meta.json."""
    assert ReplayMeta.load(tmp_path) is None
    (tmp_path / "meta.json").write_text(
        json.dumps(
            {
                "scenario": "S1",
                "start": "2026-09-26T09:23:00Z",
                "end": "2026-09-26T09:38:00Z",
                "incident_start": "2026-09-26T09:32:12Z",
            }
        )
    )
    meta = ReplayMeta.load(tmp_path)
    assert meta is not None
    assert meta.incident_start == datetime(2026, 9, 26, 9, 32, 12, tzinfo=UTC)
    window = TimeRange(start=meta.start, end=meta.end)
    task = BY_ID["S1"].task("metrics", meta.end, window)
    assert task.context.time_range == window
    # without an explicit window, the scenario's own window (30m) ends at `now`
    default = BY_ID["S1"].task("metrics", meta.end)
    assert default.context.time_range.duration.total_seconds() == 30 * 60
