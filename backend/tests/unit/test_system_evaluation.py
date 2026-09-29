"""PR-040: `aiops evaluate`, the full-system evaluation. Replay only: zero tokens."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiops.cli.main import app
from aiops.core.config import (
    CostConfig,
    LLMConfig,
    ModelPrice,
    Settings,
    load_settings,
)
from aiops.core.models import TokenUsage
from aiops.evals.investigation import InvestigationEval
from aiops.evals.runner import EvalError, EvalPaths
from aiops.evals.scoring import Check
from aiops.evals.system import (
    PLANNER_CASES_FILE,
    Baseline,
    SystemReport,
    baseline_of,
    compare,
    cost_usd,
    load_baseline,
    load_planner_cases,
    price_for,
    render_markdown,
    report_json,
    run_system_eval,
    write_report,
)
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.conftest import REPO_ROOT

cli = CliRunner()
PATHS = EvalPaths.discover(REPO_ROOT / "config")
BASELINE = REPO_ROOT / "evals" / "baselines" / "replay.json"


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", REPO_ROOT / "config")


@pytest.fixture(scope="module")
def report(settings: Settings) -> SystemReport:
    return asyncio.run(run_system_eval(settings, paths=PATHS))


def without_key(settings: Settings) -> Settings:
    return settings.model_copy(update={"llm": LLMConfig(provider="fake")})


# --------------------------------------------------------------------------- the whole system


def test_one_run_covers_agents_planner_and_investigations(report: SystemReport) -> None:
    assert {a.agent for a in report.agents} == {
        "logs",
        "metrics",
        "alerts",
        "k8s",
        "code",
        "tickets",
        "knowledge",
    }
    assert all(a.pass_rate == 1.0 and a.false_positive_rate == 0.0 for a in report.agents)
    assert all(a.citation_validity == 1.0 and a.total_tokens == 0 for a in report.agents)
    inv = report.investigation_summary
    assert inv is not None
    assert [r.scenario for r in report.investigations] == ["S0", "S1", "S2", "S3", "S4", "S5"]
    # MASTER_PLAN §15 targets (replay): >= 4/5 root causes, 0 false positives on S0
    assert inv.root_cause_correct == inv.incidents == 5 and inv.root_cause_accuracy == 1.0
    assert inv.false_positives == 0 and inv.false_positive_rate == 0.0
    assert inv.citation_validity >= 0.95
    assert 0 <= inv.brier_score < 0.05  # confident and right
    assert inv.total_tokens == 0 and inv.total_cost_usd == 0.0
    assert inv.p50_time_to_report_ms > 0


def test_negative_and_ambiguous_questions_are_never_guessed(report: SystemReport) -> None:
    planner = {p.case: p for p in report.planner}
    for case in ("N1", "N2", "N3", "N4"):
        p = planner[case]
        assert p.passed, (case, [c for c in p.checks if not c.passed])
        assert p.clarification and p.service is None
    assert set(planner["N4"].candidates) >= {"order-service", "user-service"}
    assert planner["P1"].passed and planner["P1"].service == "order-service"
    summary = report.planner_summary
    assert summary is not None and summary.invented_services == 0
    assert summary.clarification_accuracy == 1.0
    # every scenario question is scored too (S0-S5 from the question alone)
    assert {"S0", "S1", "S2", "S3", "S4", "S5"} <= set(planner)


def test_reproducibility_header(report: SystemReport) -> None:
    info = report.info
    assert info.mode == "replay" and info.profile == "local"
    assert info.llm_used.startswith("scripted replay LLM")
    assert info.git_sha and info.git_sha != "unknown"
    assert info.agent_versions["logs"] and "rca" in info.agent_versions
    # prompt versions include the provider fragments
    assert "+providers/logs/" in info.prompts["logs"]
    markdown = render_markdown(report)
    for text in ("Git SHA", "Profile | `local`", "prompt `logs`", "agent `rca`", "Brier"):
        assert text in markdown


def test_report_files(report: SystemReport, tmp_path: Path) -> None:
    markdown, json_path = write_report(report, tmp_path, regressions=[])
    assert markdown.name.endswith("-system-replay.md") and json_path.suffix == ".json"
    assert "Regression gate" in markdown.read_text() and "PASS" in markdown.read_text()
    payload = json.loads(json_path.read_text())
    assert payload["regressions"] == []
    assert payload["investigations"][1]["root_cause_correct"] is True
    assert "brier" in payload["investigations"][0]
    assert payload["info"]["prompts"]["metrics"].startswith("metrics/")
    assert json.loads(report_json(report))["planner"][0]["passed"] in (True, False)


# --------------------------------------------------------------------------- regression gate


def test_committed_baseline_holds(report: SystemReport) -> None:
    """The CI gate: this replay is not worse than evals/baselines/replay.json."""
    baseline = load_baseline(BASELINE)
    assert baseline is not None and baseline.mode == "replay"
    assert compare(report, baseline) == []


def test_gate_flags_drops_and_rises(report: SystemReport) -> None:
    base = baseline_of(report)
    assert compare(report, base) == []
    worse = Baseline(
        mode="replay",
        agents={"logs": {"pass_rate": 1.0, "false_positive_rate": -0.5}},
        investigations={"root_cause_accuracy": 1.0, "false_positive_rate": -0.1},
        planner={"pass_rate": 1.0},
    )
    found = {(r.scope, r.metric) for r in compare(report, worse)}
    # FP rate rose (0 > -0.5 / -0.1); planner pass rate dropped (S4 asks to clarify)
    assert ("agent logs", "false_positive_rate") in found
    assert ("investigations", "false_positive_rate") in found
    assert ("planner", "pass_rate") in found
    assert ("investigations", "root_cause_accuracy") not in found
    # a tolerance absorbs small moves
    assert not compare(report, Baseline(mode="replay", planner={"pass_rate": 0.95}), 0.05)
    # agents that weren't run are not compared
    assert not compare(report, Baseline(mode="replay", agents={"nope": {"pass_rate": 1.0}}))


# --------------------------------------------------------------------------- scorers


def test_brier_and_root_cause_accuracy() -> None:
    def ev(healthy: bool, correct: bool, confidence: float) -> InvestigationEval:
        name = "no_incident:no_hypothesis" if healthy else "root_cause_identified"
        return InvestigationEval(
            scenario="X",
            title="x",
            investigation_id="inv-x",
            status="completed",
            healthy=healthy,
            root_cause=None if healthy and correct else "something",
            confidence=confidence,
            severity="none" if healthy and correct else "high",
            checks=[Check(name=name, passed=correct)],
        )

    assert ev(False, True, 0.9).brier == pytest.approx(0.01)
    assert ev(False, False, 0.9).brier == pytest.approx(0.81)  # confidently wrong
    assert ev(True, True, 0.0).brier == 0.0
    healthy_fp = ev(True, False, 0.6)
    assert healthy_fp.false_positive and healthy_fp.brier == pytest.approx(0.36)


def test_cost_from_the_price_table(settings: Settings) -> None:
    priced = settings.model_copy(
        update={
            "cost": CostConfig(  # PR-041: the price table moved from evals.pricing to cost.pricing
                pricing={
                    "big-model": ModelPrice(input=3.0, output=15.0),
                    "api.groq.com": ModelPrice(input=0.5, output=1.0),
                }
            )
        }
    )
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=100_000)
    assert cost_usd(price_for(priced, "big-model"), usage) == pytest.approx(4.5)
    assert cost_usd(price_for(priced, "other"), usage) == pytest.approx(0.6)  # provider host
    assert cost_usd(price_for(settings, "any"), usage) == 0.0  # free tiers by default


def test_llm_judge_is_skipped_without_a_key(settings: Settings) -> None:
    report = asyncio.run(
        run_system_eval(
            without_key(settings), agents=[], judge=True, scenario_ids=["S1"], paths=PATHS
        )
    )
    assert report.info.judge.startswith("skipped")
    assert any("LLM judge skipped" in n for n in report.notes)
    assert report.investigations[0].judge is None
    assert report.investigation_summary is not None
    assert report.investigation_summary.judge_accuracy is None


def test_llm_judge_scores_root_causes(settings: Settings) -> None:
    judge = FakeLLMProvider(
        responder=lambda _m, _t: tool_call(
            "submit", {"correct": True, "score": 0.9, "reason": "Same cause."}
        )
    )
    report = asyncio.run(
        run_system_eval(
            settings,
            agents=[],
            judge=True,
            scenario_ids=["S0", "S1"],
            paths=PATHS,
            judge_factory=lambda: judge,
        )
    )
    by_id = {r.scenario: r for r in report.investigations}
    assert by_id["S0"].judge is None  # healthy: nothing to judge
    s1 = by_id["S1"].judge
    assert s1 is not None and s1.correct
    assert report.investigation_summary is not None
    assert report.investigation_summary.judge_accuracy == 1.0
    assert report.info.judge.startswith("on (judge/v1@")
    # the ground truth reached the judge
    prompt = judge.requests[0]["messages"][0].content
    assert "DB_POOL_SIZE 20 -> 2" in prompt


def test_planner_cases_file_is_valid(settings: Settings) -> None:
    from aiops.core.catalog import ServiceCatalog

    cases = load_planner_cases(PATHS.scenarios / PLANNER_CASES_FILE)
    assert len(cases) >= 4 and any(c.expect == "clarification" for c in cases)
    known = {s.name for s in ServiceCatalog.from_settings(settings).services}
    for case in cases:
        assert case.service is None or case.service in known
        assert not set(case.never) & known  # "never invented" names aren't real services


def test_unknown_selection_is_an_error(settings: Settings) -> None:
    with pytest.raises(EvalError, match="Unknown agent"):
        asyncio.run(run_system_eval(settings, agents=["nope"], investigations=False, paths=PATHS))
    with pytest.raises(EvalError, match="Unknown scenario"):
        asyncio.run(run_system_eval(settings, scenario_ids=["S9"], paths=PATHS))


def test_live_needs_a_key(settings: Settings) -> None:
    with pytest.raises(EvalError, match="hosted LLM"):
        asyncio.run(
            run_system_eval(
                without_key(settings),
                agents=["logs"],
                investigations=False,
                mode="live",
                paths=PATHS,
            )
        )


# --------------------------------------------------------------------------- CLI


def test_cli_agents_only(tmp_path: Path) -> None:
    result = cli.invoke(
        app, ["evaluate", "--agents", "logs,metrics", "--out", str(tmp_path), "--profile", "local"]
    )
    assert result.exit_code == 0, result.output
    assert "Regression gate: no metric is worse" in result.output
    written = list(tmp_path.glob("*-system-replay.md"))
    assert written and "## Agents" in written[0].read_text()
    assert "## Investigations" not in written[0].read_text()


def test_cli_gate_fails_on_regression(tmp_path: Path) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        Baseline(mode="replay", planner={"pass_rate": 1.0}).model_dump_json()  # S4 asks
    )
    result = cli.invoke(
        app, ["evaluate", "--investigations", "--baseline", str(baseline), "--no-write"]
    )
    assert result.exit_code == 1, result.output
    assert "planner: pass_rate" in result.output
    result = cli.invoke(
        app,
        ["evaluate", "--investigations", "--baseline", str(baseline), "--no-write", "--no-gate"],
    )
    assert result.exit_code == 0, result.output


def test_cli_update_baseline(tmp_path: Path) -> None:
    baseline = tmp_path / "b.json"
    result = cli.invoke(
        app,
        [
            "evaluate",
            "--agents",
            "logs",
            "--baseline",
            str(baseline),
            "--update-baseline",
            "--no-write",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(baseline.read_text())
    assert data["mode"] == "replay" and data["agents"]["logs"]["pass_rate"] == 1.0
