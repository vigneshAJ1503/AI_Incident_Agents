"""The ``code`` capability: what the Code agent asks, and the neutral change shapes.

Neutral result shapes (git-mcp already returns them; GitHub/GitLab adapters map to them):

* releases: ``[{tag, sha, date (ISO), ...}]``
* commits: ``[{sha, short, message, author, date (ISO), files: [{path, ...}]}]``
* diff files: ``[{path, status, hunks/patch, ...}]`` as consumed by ``code_agent.analysis``
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, ClassVar

from aiops.providers.base import Provider, ToolRequest


def _list(data: Any, key: str) -> list[Any]:
    return list((data or {}).get(key, []))


class CodeProvider(Provider):
    """Interface of a ``code`` provider (override every ``NotImplementedError``)."""

    capability: ClassVar[str] = "code"

    def releases_of(self, repo: str, limit: int) -> ToolRequest:
        """The latest release tags of ``repo``."""
        raise NotImplementedError

    def commits_touching(
        self, repo: str, since: datetime, until: datetime, paths: Sequence[str], limit: int
    ) -> ToolRequest:
        """Commits in ``[since, until]`` touching any of ``paths`` (with their files)."""
        raise NotImplementedError

    def diff_of(self, repo: str, sha: str, paths: Sequence[str]) -> ToolRequest:
        """The diff of one commit, limited to ``paths``."""
        raise NotImplementedError

    def releases(self, data: Any) -> list[Any]:
        return _list(data, "releases")

    def commits(self, data: Any) -> list[Any]:
        return _list(data, "commits")

    def diff_files(self, data: Any) -> list[Any]:
        return _list(data, "files")
