from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from git_mcp.config import ServerSettings

T0 = "2026-09-24T10:00:00+00:00"


def git(repo: Path, *args: str, date: str = T0) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Dev",
        GIT_AUTHOR_EMAIL="dev@example.com",
        GIT_COMMITTER_NAME="Dev",
        GIT_COMMITTER_EMAIL="dev@example.com",
        GIT_AUTHOR_DATE=date,
        GIT_COMMITTER_DATE=date,
    )
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-C", str(repo), *args],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str, date: str) -> str:
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    git(repo, "add", "-A", date=date)
    git(repo, "commit", "-q", "-m", message, date=date)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture(scope="session")
def repo(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """A small repo: 4 commits over 2 days, two release tags. Returns name -> sha."""
    root = tmp_path_factory.mktemp("repos") / "sample-repo"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    shas = {"root": str(root)}
    shas["init"] = commit(
        root,
        {
            "services/payment-service/config/app.yaml": 'env:\n  DB_POOL_SIZE: "20"\n',
            "services/order-service/app.py": "print('orders')\n",
            "README.md": "# repo\n",
        },
        "initial layout",
        "2026-09-23T09:00:00+00:00",
    )
    git(
        root,
        "tag",
        "-a",
        "payment-service/v1.8.1",
        "-m",
        "v1.8.1",
        date="2026-09-23T09:00:00+00:00",
    )
    shas["docs"] = commit(
        root, {"README.md": "# repo\n\nDocs.\n"}, "docs: readme", "2026-09-24T12:00:00+00:00"
    )
    shas["pool"] = commit(
        root,
        {"services/payment-service/config/app.yaml": 'env:\n  DB_POOL_SIZE: "2"\n'},
        "tune db pool",
        "2026-09-25T08:20:00+00:00",
    )
    shas["orders"] = commit(
        root,
        {"services/order-service/app.py": "print('orders v2')\n" + "x = 1\n" * 50},
        "order-service: refactor",
        "2026-09-25T09:00:00+00:00",
    )
    git(
        root,
        "tag",
        "-a",
        "payment-service/v1.8.2",
        "-m",
        "v1.8.2",
        shas["pool"],
        date="2026-09-25T09:55:00+00:00",
    )
    return shas


@pytest.fixture
def settings(repo: dict[str, str]) -> ServerSettings:
    return ServerSettings(repos={"sample-repo": Path(repo["root"])}, max_commits=3)
