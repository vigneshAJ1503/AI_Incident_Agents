"""Server settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def parse_repos(spec: str) -> dict[str, Path]:
    """``"sample-repo=/repos/sample-repo,other=/srv/other"`` -> {name: path}."""
    repos: dict[str, Path] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, path = item.partition("=")
        if not sep or not name.strip() or not path.strip():
            raise ValueError(f"GIT_REPOS entry must be name=/abs/path (got '{item}')")
        repos[name.strip()] = Path(path.strip())
    return repos


@dataclass(frozen=True)
class ServerSettings:
    #: Allowlist of repositories: logical name -> root directory. Nothing else is reachable.
    repos: dict[str, Path] = field(default_factory=dict)
    max_commits: int = 200
    max_diff_chars: int = 40_000
    max_files: int = 200
    max_range_days: float = 90.0
    command_timeout_s: float = 15.0
    git_binary: str = "git"

    @classmethod
    def from_env(cls) -> ServerSettings:
        return cls(
            repos=parse_repos(_env("GIT_REPOS", "sample-repo=/repos/sample-repo")),
            max_commits=int(_env("MAX_COMMITS", "200")),
            max_diff_chars=int(_env("MAX_DIFF_CHARS", "40000")),
            max_files=int(_env("MAX_FILES", "200")),
            max_range_days=float(_env("MAX_RANGE_DAYS", "90")),
            command_timeout_s=float(_env("COMMAND_TIMEOUT_S", "15")),
            git_binary=_env("GIT_BINARY", "git"),
        )
