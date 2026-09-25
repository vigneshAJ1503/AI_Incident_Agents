"""Sample Git repo (PR-026): deterministic SHAs, scenario commits, safe rebuilds."""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aiops.cli.main import app
from aiops.seed.git_repo import (
    BACKGROUND,
    SCENARIO_CHANGES,
    RepoSeedError,
    RepoSummary,
    build_sample_repo,
    changes_for,
)
from aiops.seed.logs import SCENARIOS, SERVICES

NOW = datetime(2026, 9, 25, 10, 30, tzinfo=UTC)
INCIDENT_START = NOW - timedelta(minutes=20)
DEPLOY = NOW - timedelta(minutes=22)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - test helper, fixed argv
        ["git", "-c", f"safe.directory={repo}", "-C", str(repo), *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def build(tmp_path: Path, scenario: str, name: str | None = None) -> RepoSummary:
    return build_sample_repo(tmp_path / (name or scenario), scenario, NOW)


@pytest.fixture(scope="module")
def repos(tmp_path_factory: pytest.TempPathFactory) -> dict[str, RepoSummary]:
    """Each scenario built once for the read-only tests."""
    root = tmp_path_factory.mktemp("repos")
    return {s: build(root, s) for s in SCENARIOS}


def by_subject(summary: RepoSummary, subject: str) -> str:
    return next(c.sha for c in summary.commits if c.subject == subject)


def test_same_now_gives_same_shas(tmp_path: Path) -> None:
    first = build(tmp_path, "S1", "a")
    second = build(tmp_path, "S1", "b")
    assert [c.sha for c in first.commits] == [c.sha for c in second.commits]
    assert first.tags == second.tags
    assert git(first.path, "rev-parse", "payment-service/v1.8.2") == git(
        second.path, "rev-parse", "payment-service/v1.8.2"
    )  # annotated tag objects are deterministic too
    shifted = build_sample_repo(tmp_path / "c", "S1", NOW + timedelta(hours=1))
    assert shifted.head != first.head


def test_history_shape(repos: dict[str, RepoSummary]) -> None:
    summary = repos["S0"]
    assert 28 <= len(summary.commits) <= 36
    dates = [c.date for c in summary.commits]
    assert dates == sorted(dates)
    assert NOW - timedelta(days=15) < dates[0] < NOW - timedelta(days=13)
    assert dates[-1] < NOW
    assert len({c.author for c in summary.commits}) >= 5
    files = git(summary.path, "ls-files").splitlines()
    for service in SERVICES:
        for rel in ("config/app.yaml", "k8s/deployment.yaml", "requirements.txt", "app/main.py"):
            assert f"services/{service}/{rel}" in files
    # Commit metadata matches the declared timeline (UTC, author).
    log = git(summary.path, "log", "-1", "--format=%an|%cI", summary.commits[0].sha)
    assert log == f"Sam Okafor|{summary.commits[0].date.isoformat().replace('+00:00', 'Z')}"


def test_release_tags_match_log_versions(repos: dict[str, RepoSummary]) -> None:
    """Tags `<service>/<version>` line up with the versions the synthetic logs report."""
    summary = repos["S0"]
    for service, profile in SERVICES.items():
        assert f"{service}/{profile.version}" in summary.tags
        manifest = git(summary.path, "show", f"HEAD:services/{service}/k8s/deployment.yaml")
        assert f"{service}:{profile.version}" in manifest
    assert "payment-service/v1.8.2" not in summary.tags  # S0: payment still runs v1.8.1


def test_s1_tune_db_pool(repos: dict[str, RepoSummary]) -> None:
    summary = repos["S1"]
    sha = by_subject(summary, "tune db pool")
    commit = next(c for c in summary.commits if c.sha == sha)
    assert DEPLOY - timedelta(hours=3) < commit.date < DEPLOY  # a couple of hours before rollout
    diff = git(summary.path, "show", "--format=", sha)
    assert '-  DB_POOL_SIZE: "20"' in diff and '+  DB_POOL_SIZE: "2"' in diff
    assert "services/payment-service/config/app.yaml" in diff
    release = summary.tags["payment-service/v1.8.2"]
    assert git(summary.path, "merge-base", "--is-ancestor", sha, release) == ""
    released = next(c for c in summary.commits if c.sha == release)
    assert commit.date < released.date < DEPLOY
    manifest = git(summary.path, "show", "HEAD:services/payment-service/k8s/deployment.yaml")
    assert "payment-service:v1.8.2" in manifest


def test_s2_unbounded_cache_and_memory_limit(repos: dict[str, RepoSummary]) -> None:
    summary = repos["S2"]
    cache = git(
        summary.path,
        "show",
        "--format=",
        by_subject(summary, "order-service: cache order payloads in memory"),
    )
    assert "_ORDER_CACHE[order_id] = payload  # no eviction" in cache
    limit = git(
        summary.path,
        "show",
        "--format=",
        by_subject(summary, "order-service: right-size memory limit"),
    )
    assert "-              memory: 1Gi" in limit and "+              memory: 512Mi" in limit


def test_s3_inventory_query_drops_index(repos: dict[str, RepoSummary]) -> None:
    summary = repos["S3"]
    diff = git(
        summary.path,
        "show",
        "--format=",
        by_subject(summary, "inventory-service: simplify stock level query"),
    )
    assert "DROP INDEX IF EXISTS idx_stock_levels_sku" in diff
    assert "lower(sku) = lower(%s)" in diff


def test_s4_image_tag_has_no_release(repos: dict[str, RepoSummary]) -> None:
    summary = repos["S4"]
    sha = by_subject(summary, "deploy user-service v3.2.0")
    commit = next(c for c in summary.commits if c.sha == sha)
    assert NOW - timedelta(hours=1) < commit.date < INCIDENT_START
    diff = git(summary.path, "show", "--format=", sha)
    assert "+          image: registry.example.com/acme/user-service:v3.2.0" in diff
    assert "user-service/v3.2.0" not in summary.tags  # the image was never built/released


@pytest.mark.parametrize("scenario", ["S0", "S5"])
def test_healthy_and_infra_scenarios_have_no_incident_change(
    repos: dict[str, RepoSummary], scenario: str
) -> None:
    summary = repos[scenario]
    assert all(c.scenario is None for c in summary.commits)
    assert len(summary.commits) == len(BACKGROUND)


def test_every_incident_scenario_has_changes() -> None:
    assert {c.scenario for c in SCENARIO_CHANGES} == {"S1", "S2", "S3", "S4"}
    for scenario in SCENARIOS:
        changes = changes_for(scenario)
        ages = [c.age for c in changes]
        assert ages == sorted(ages, reverse=True)
    with pytest.raises(RepoSeedError, match="Unknown scenario"):
        changes_for("S9")


def test_rebuild_in_place_but_never_overwrite_foreign_dirs(tmp_path: Path) -> None:
    target = tmp_path / "repo"
    build_sample_repo(target, "S1", NOW)
    inode = target.stat().st_ino
    summary = build_sample_repo(target, "S0", NOW)  # switch scenario
    assert target.stat().st_ino == inode  # same directory: read-only bind mounts keep working
    assert "tune db pool" not in git(target, "log", "--format=%s")
    assert git(target, "status", "--porcelain") == ""
    assert summary.head == git(target, "rev-parse", "HEAD")

    foreign = tmp_path / "precious"
    foreign.mkdir()
    (foreign / "notes.txt").write_text("keep me")
    with pytest.raises(RepoSeedError, match="refusing to overwrite"):
        build_sample_repo(foreign, "S1", NOW)
    assert (foreign / "notes.txt").read_text() == "keep me"


def test_ignores_hostile_git_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GIT_DIR etc. leak in from git hooks; the builder must not write elsewhere."""
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Someone Else")
    summary = build(tmp_path, "S1")
    assert not elsewhere.exists()
    assert "Someone Else" not in {c.author for c in summary.commits}


def test_cli_seed_repo(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        app,
        [
            "seed",
            "repo",
            "-S",
            "s1",
            "--now",
            "2026-09-25T10:30:00",
            "--path",
            str(tmp_path / "repo"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "tune db pool" in result.output
    expected = build_sample_repo(tmp_path / "again", "S1", NOW).head
    assert git(tmp_path / "repo", "rev-parse", "HEAD") == expected  # naive --now is UTC
