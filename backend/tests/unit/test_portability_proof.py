"""The portability proof, enforced (PR-P4a): adding a second logs backend (Loki) changed
NO agent and NO orchestrator code.

Checked from git history, so it stays true after merge and never blocks later PRs:

* on the PR itself (the adapter is absent at the merge base with ``origin/main``): the diff
  ``merge-base..HEAD`` must not touch ``backend/src/aiops/{agents,orchestrator}``;
* afterwards: the commit that ADDED ``providers/logs/loki.py`` (the squash-merged PR) must
  not touch them either.

CI checks out full history (``fetch-depth: 0``); in a shallow clone the test is skipped.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
ADAPTER = "backend/src/aiops/providers/logs/loki.py"
PROTECTED = ["backend/src/aiops/agents", "backend/src/aiops/orchestrator"]


def git(*args: str) -> str:
    result = subprocess.run(  # noqa: S603 - fixed git arguments
        ["git", *args],  # noqa: S607 - git from PATH
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise LookupError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def exists_at(rev: str, path: str) -> bool:
    try:
        git("cat-file", "-e", f"{rev}:{path}")
    except LookupError:
        return False
    return True


def proof_range() -> str:
    """The revision range that introduced the Loki adapter."""
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    try:
        if git("rev-parse", "--is-shallow-repository") == "true":
            pytest.skip("shallow clone: history needed (CI uses fetch-depth: 0)")
    except LookupError:
        pytest.skip("not a git checkout")
    try:
        base = git("merge-base", "HEAD", "origin/main")
    except LookupError:
        base = ""
    if base and not exists_at(base, ADAPTER) and exists_at("HEAD", ADAPTER):
        return f"{base}..HEAD"  # the PR that adds it
    added = git("log", "--diff-filter=A", "--format=%H", "--", ADAPTER).splitlines()
    if not added:
        pytest.skip(f"{ADAPTER} is not committed yet")
    commit = added[-1]
    return f"{commit}^..{commit}"


def test_adding_the_loki_provider_changed_no_agent_or_orchestrator_code() -> None:
    rng = proof_range()
    changed = git("diff", "--name-only", rng, "--", *PROTECTED)
    assert changed == "", f"{rng} changed agent/orchestrator code:\n{changed}"
    added = git("diff", "--name-only", rng, "--", ADAPTER)
    assert added == ADAPTER  # the range really is the one that introduced the adapter
