"""``code/git``: our read-only git-mcp over local clones (mirror GitHub/GitLab repos)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import ClassVar

from aiops.providers.base import ToolRequest, iso
from aiops.providers.code import CodeProvider
from aiops.providers.registry import PROVIDER_REGISTRY

LIST_RELEASES = "list_releases"
SEARCH_COMMITS = "search_commits"
GET_DIFF = "get_diff"


class GitCode(CodeProvider):
    name: ClassVar[str] = "git"
    mcp: ClassVar[str] = "mcp-servers/git-mcp"
    agent_tools: ClassVar[tuple[str, ...]] = (LIST_RELEASES, SEARCH_COMMITS, GET_DIFF)
    note: ClassVar[str] = "local clones (mirror your GitHub/GitLab repos read-only)"
    prompt_fragment: ClassVar[str | None] = "providers/code/git"

    def releases_of(self, repo: str, limit: int) -> ToolRequest:
        return ToolRequest(LIST_RELEASES, {"repo": repo, "limit": limit})

    def commits_touching(
        self, repo: str, since: datetime, until: datetime, paths: Sequence[str], limit: int
    ) -> ToolRequest:
        return ToolRequest(
            SEARCH_COMMITS,
            {
                "repo": repo,
                "since": iso(since),
                "until": iso(until),
                "paths": list(paths),
                "limit": limit,
            },
        )

    def diff_of(self, repo: str, sha: str, paths: Sequence[str]) -> ToolRequest:
        return ToolRequest(GET_DIFF, {"repo": repo, "sha": sha, "paths": list(paths)})


PROVIDER_REGISTRY.register(GitCode)
