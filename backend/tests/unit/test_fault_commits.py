"""Demo fault injection also commits the change to the sample Git repo (git-mcp reads it)."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiops.cli.main import app
from aiops.fault_commits import SWITCH_VAR, DemoRepo
from aiops.faults import FaultError, FaultInjector, FaultState
from aiops.seed.git_repo import RepoBuilder, build_sample_repo, repo_marker

SEEDED = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 29, 10, 15, 42, tzinfo=UTC)
CONFIG = "services/payment-service/config/app.yaml"


def git(repo: Path, *args: str) -> str:
    return RepoBuilder(repo).git(*args)


class FakeKubectl:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def __call__(self, args: Sequence[str]) -> str:
        if self.fail:
            raise FaultError("kubectl: connection refused")
        return ""


@pytest.fixture(scope="module")
def healthy_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A healthy (S0) sample repo, built once; tests copy it."""
    return build_sample_repo(tmp_path_factory.mktemp("seed") / "sample-repo", "S0", SEEDED).path


def setup(tmp_path: Path, healthy_repo: Path, kubectl: FakeKubectl | None = None) -> FaultInjector:
    target = tmp_path / ".data" / "sample-repo"
    target.parent.mkdir(parents=True)
    shutil.copytree(healthy_repo, target, symlinks=True)
    repo = DemoRepo(target, clock=lambda: NOW, log=lambda _: None)
    return FaultInjector(
        tmp_path, kubectl or FakeKubectl(), sleep=lambda _: None, log=lambda _: None, code_repo=repo
    )


def test_s1_injection_commits_the_pool_change_like_the_fixtures(
    tmp_path: Path, healthy_repo: Path
) -> None:
    inj = setup(tmp_path, healthy_repo)
    repo = tmp_path / ".data" / "sample-repo"
    head_before = git(repo, "rev-parse", "HEAD")
    state = inj.inject("S1", wait=False)

    assert len(state.commits) == 2
    assert FaultState.load(inj.state_path).commits == state.commits
    log = git(repo, "log", "-3", "--format=%H|%an|%aI|%s")
    rows = [line.split("|") for line in log.splitlines()]
    assert [r[3] for r in rows] == [
        "release payment-service v1.8.2",
        "tune db pool",
        git(repo, "log", "-1", "--format=%s", head_before),
    ]
    tune = rows[1]
    assert tune[0] == state.commits[0] and tune[1] == "Jordan Lee"
    assert datetime.fromisoformat(tune[2]) <= NOW  # "now", not the seed's anchor
    assert rows[0][2] == "2026-09-29T10:15:42Z"
    diff = git(repo, "show", state.commits[0], "--", CONFIG)
    assert '-  DB_POOL_SIZE: "20"' in diff and '+  DB_POOL_SIZE: "2"' in diff
    assert git(repo, "rev-list", "-n1", "payment-service/v1.8.2") == state.commits[1]
    assert git(repo, "status", "--porcelain") == ""
    assert git(repo, "remote") == ""  # nothing to push to, ever


def test_revert_commits_a_git_revert(tmp_path: Path, healthy_repo: Path) -> None:
    inj = setup(tmp_path, healthy_repo)
    repo = tmp_path / ".data" / "sample-repo"
    inj.inject("S1", wait=False)
    inj.revert()

    subjects = git(repo, "log", "-2", "--format=%s").splitlines()
    assert subjects == ['Revert "tune db pool"', 'Revert "release payment-service v1.8.2"']
    assert 'DB_POOL_SIZE: "20"' in (repo / CONFIG).read_text()
    assert git(repo, "status", "--porcelain") == ""
    assert FaultState.load(inj.state_path) == FaultState()

    again = inj.inject("S1", wait=False)  # the demo can be replayed (release tag moves)
    assert len(again.commits) == 2
    assert git(repo, "rev-list", "-n1", "payment-service/v1.8.2") == again.commits[1]


@pytest.mark.parametrize(("scenario", "commits"), [("S2", 2), ("S3", 1), ("S4", 1), ("S5", 0)])
def test_other_scenarios(tmp_path: Path, healthy_repo: Path, scenario: str, commits: int) -> None:
    inj = setup(tmp_path, healthy_repo)
    state = inj.inject(scenario, wait=False)
    assert len(state.commits) == commits  # S5 (Redis outage) has no code/config change
    inj.revert()
    assert FaultState.load(inj.state_path).commits == []


def test_failed_rollout_reverts_the_commits(tmp_path: Path, healthy_repo: Path) -> None:
    inj = setup(tmp_path, healthy_repo, FakeKubectl(fail=True))
    repo = tmp_path / ".data" / "sample-repo"
    with pytest.raises(FaultError):
        inj.inject("S1", wait=False)
    assert git(repo, "log", "-1", "--format=%s") == 'Revert "tune db pool"'
    assert 'DB_POOL_SIZE: "20"' in (repo / CONFIG).read_text()


def test_repo_already_seeded_for_the_scenario_gets_no_empty_commit(tmp_path: Path) -> None:
    repo = build_sample_repo(tmp_path / ".data" / "sample-repo", "S1", SEEDED).path
    head = git(repo, "rev-parse", "HEAD")
    messages: list[str] = []
    commits = DemoRepo(repo, clock=lambda: NOW, log=messages.append).commit_fault("S1")
    assert commits == [] and git(repo, "rev-parse", "HEAD") == head
    assert "'tune db pool' is already in" in messages[0] and "make seed-repo S=S0" in messages[0]


def test_never_touches_a_repo_it_did_not_generate(tmp_path: Path) -> None:
    repo = tmp_path / "real-repo"
    repo.mkdir()
    git(repo, "init", "-q")
    messages: list[str] = []
    assert DemoRepo(repo, log=messages.append).commit_fault("S1") == []
    assert "not generated by `aiops seed repo`" in messages[0]
    missing = DemoRepo(tmp_path / "nope", log=messages.append)
    assert missing.commit_fault("S1") == [] and "make seed-repo" in messages[1]


def test_missing_repo_never_blocks_the_injection(tmp_path: Path) -> None:
    repo = DemoRepo(tmp_path / ".data" / "sample-repo", log=lambda _: None)
    inj = FaultInjector(
        tmp_path, FakeKubectl(), sleep=lambda _: None, log=lambda _: None, code_repo=repo
    )
    assert inj.inject("S1", wait=False).commits == []


def test_seed_repo_if_older_than_keeps_a_fresh_healthy_repo(tmp_path: Path) -> None:
    """make demo-live: rebuild a healthy S0 repo unless one from the last 12 h exists
    (so injected commits are not wiped by every restart)."""
    target = tmp_path / "sample-repo"
    args = ["seed", "repo", "-S", "S0", "--path", str(target), "--if-older-than", "12"]
    first = CliRunner().invoke(app, [*args, "--now", "2026-09-29T09:00:00"])
    assert first.exit_code == 0, first.output
    assert repo_marker(target) == ("S0", SEEDED)
    head = git(target, "rev-parse", "HEAD")
    kept = CliRunner().invoke(app, [*args, "--now", "2026-09-29T20:00:00"])
    assert "kept" in kept.output and git(target, "rev-parse", "HEAD") == head
    rebuilt = CliRunner().invoke(app, [*args, "--now", "2026-09-29T21:30:00"])
    assert rebuilt.exit_code == 0 and git(target, "rev-parse", "HEAD") != head
    other = CliRunner().invoke(app, [*args[:3], "S1", *args[4:], "--now", "2026-09-29T21:31:00"])
    assert other.exit_code == 0 and repo_marker(target)[0] == "S1"  # type: ignore[index]


def test_opt_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert DemoRepo.for_root(tmp_path) is not None
    assert DemoRepo.for_root(tmp_path).path == tmp_path / ".data" / "sample-repo"  # type: ignore[union-attr]
    monkeypatch.setenv(SWITCH_VAR, "0")
    assert DemoRepo.for_root(tmp_path) is None
