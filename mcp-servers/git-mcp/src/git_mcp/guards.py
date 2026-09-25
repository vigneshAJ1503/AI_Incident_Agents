"""Server-side guardrails: repo allowlist, path/ref validation, time-range and size limits.

Enforced in the MCP server itself so that *any* client (not only our agents)
gets the same protection. Every value that reaches a git command line is
validated here first, and git is always called with a fixed argv (no shell).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

# Refs: branch/tag names or (abbreviated) SHAs. No leading '-', no '..', no revision
# syntax (^, ~, @{, :), no whitespace or glob characters.
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")
_FORBIDDEN_PATH_CHARS = re.compile(r"[\x00-\x1f*?\[\]\\]")
MAX_PATHS = 20
MAX_GREP_CHARS = 200


class GuardError(ValueError):
    """A request violates a guardrail. The message is shown to the model."""


@dataclass(frozen=True)
class TimeWindow:
    since: datetime
    until: datetime

    @staticmethod
    def _git(ts: datetime) -> str:
        return ts.strftime("%Y-%m-%dT%H:%M:%S+00:00")

    @property
    def git_since(self) -> str:
        return self._git(self.since)

    @property
    def git_until(self) -> str:
        return self._git(self.until)


def validate_repo(name: str, repos: dict[str, Path]) -> Path:
    """Resolve a logical repo name from the allowlist to its root directory."""
    if name not in repos:
        allowed = ", ".join(sorted(repos)) or "none configured"
        raise GuardError(f"repo '{name}' is not allowed (allowed: {allowed})")
    root = repos[name].resolve()
    if not (root / ".git").exists():
        raise GuardError(f"repo '{name}' is not available (no git repository at its root)")
    return root


def validate_paths(paths: list[str] | None, root: Path) -> list[str]:
    """Relative paths inside the repo; no traversal, absolute paths or pathspec magic."""
    if not paths:
        return []
    if len(paths) > MAX_PATHS:
        raise GuardError(f"at most {MAX_PATHS} paths are allowed")
    clean: list[str] = []
    for raw in paths:
        path = raw.strip()
        if not path:
            continue
        if path.startswith(("/", "-", ":")) or _FORBIDDEN_PATH_CHARS.search(path):
            raise GuardError(f"path '{raw}' is not allowed: use a plain path relative to the repo")
        parts = PurePosixPath(path).parts
        if ".." in parts or ".git" in parts:
            raise GuardError(f"path '{raw}' is not allowed: it leaves the repository")
        resolved = (root / path).resolve()
        if resolved != root and root not in resolved.parents:
            raise GuardError(f"path '{raw}' is not allowed: it leaves the repository")
        clean.append(path.rstrip("/"))
    return clean


def validate_ref(ref: str, name: str = "ref") -> str:
    ref = ref.strip()
    if not _REF.fullmatch(ref) or ".." in ref or ref.endswith((".lock", "/", ".")):
        raise GuardError(f"{name} '{ref}' is not a valid commit SHA or tag/branch name")
    return ref


def parse_time(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise GuardError(
            f"{name} must be an ISO-8601 timestamp, e.g. 2026-09-25T10:00:00Z (got '{value}')"
        ) from None
    if parsed.tzinfo is None:  # naive = UTC
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def validate_time_range(since: str, until: str, max_days: float) -> TimeWindow:
    start, end = parse_time(since, "since"), parse_time(until, "until")
    if end <= start:
        raise GuardError("until must be after since")
    if end - start > timedelta(days=max_days):
        raise GuardError(f"time range exceeds the maximum of {max_days:g} days")
    return TimeWindow(start, end)


def validate_grep(grep: str | None) -> str | None:
    if grep is None or not grep.strip():
        return None
    if len(grep) > MAX_GREP_CHARS or "\x00" in grep:
        raise GuardError(f"grep must be at most {MAX_GREP_CHARS} characters")
    return grep.strip()


def clamp(value: int, maximum: int, minimum: int = 1) -> int:
    return max(minimum, min(int(value), maximum))
