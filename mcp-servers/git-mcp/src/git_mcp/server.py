"""MCP tools: list_repositories, list_releases, search_commits, get_commit, get_diff (read-only)."""

from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from git_mcp.config import ServerSettings
from git_mcp.git import (
    COMMIT_FORMAT,
    FIELD,
    RECORD,
    GitError,
    GitRunner,
    cap_diff,
    parse_diff,
    parse_log,
)
from git_mcp.guards import (
    GuardError,
    clamp,
    validate_grep,
    validate_paths,
    validate_ref,
    validate_repo,
    validate_time_range,
)

INSTRUCTIONS = """Read-only access to allowlisted Git repositories.
Typical flow: search_commits for a service's paths in a time window (ISO-8601 UTC),
then get_diff for suspicious commits, and list_releases for release tags
(scheme '<service>/<version>'). Paths are relative to the repository root."""

MAX_CONTEXT_LINES = 10


def create_server(settings: ServerSettings, runner: GitRunner | None = None) -> MCPServer:
    git = runner or GitRunner(settings.git_binary, settings.command_timeout_s)
    server = MCPServer("git-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    async def guarded[T](coro: Awaitable[T]) -> T:
        try:
            return await coro
        except (GuardError, GitError) as exc:
            raise ToolError(str(exc)) from exc

    async def resolve_commit(root: Path, ref: str, name: str = "sha") -> str:
        ref = validate_ref(ref, name)
        try:
            out = await git.run(
                root, ["rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}"]
            )
        except GitError:
            raise GuardError(f"{name} '{ref}' does not name a commit in this repository") from None
        return out.strip()

    async def tag_index(root: Path) -> dict[str, list[str]]:
        out = await git.run(
            root,
            [
                "for-each-ref",
                "--format=%(refname:short)%1f%(objectname)%1f%(*objectname)",
                "refs/tags/",
            ],
        )
        index: dict[str, list[str]] = {}
        for line in out.splitlines():
            name, obj, peeled = line.split(FIELD)
            index.setdefault(peeled or obj, []).append(name)
        return index

    @server.tool()
    def list_repositories() -> dict[str, Any]:
        """Names of the repositories this server may read."""
        return {"repositories": sorted(settings.repos)}

    @server.tool()
    async def list_releases(
        repo: str, prefix: str | None = None, limit: int = 50
    ) -> dict[str, Any]:
        """Release tags, newest first, with the commit they point to and their date.

        Args:
            repo: repository name (see list_repositories), e.g. 'sample-repo'.
            prefix: optional tag prefix, e.g. 'payment-service/' (tags are '<service>/<version>').
            limit: max tags to return (capped by the server).
        """

        async def run() -> dict[str, Any]:
            root = validate_repo(repo, settings.repos)
            # for-each-ref patterns match whole path components: 'payment-service' matches
            # 'refs/tags/payment-service/*'.
            pattern = "refs/tags/"
            if prefix and prefix.strip("/"):
                pattern += validate_ref(prefix.strip("/"), "prefix")
            out = await git.run(
                root,
                [
                    "for-each-ref",
                    "--sort=-creatordate",
                    "--format=%(refname:short)%1f%(objectname)%1f%(*objectname)%1f"
                    "%(creatordate:iso-strict)%1f%(contents:subject)",
                    pattern,
                ],
            )
            releases = []
            for line in out.splitlines():
                name, obj, peeled, date, subject = line.split(FIELD)
                releases.append(
                    {"tag": name, "sha": peeled or obj, "date": date, "message": subject}
                )
            size = clamp(limit, settings.max_commits)
            return {
                "repo": repo,
                "releases": releases[:size],
                "truncated": len(releases) > size,
            }

        return await guarded(run())

    @server.tool()
    async def search_commits(
        repo: str,
        since: str,
        until: str,
        paths: list[str] | None = None,
        grep: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Commits in [since, until] (committer date), newest first, with changed files and tags.

        Args:
            repo: repository name, e.g. 'sample-repo'.
            since: window start, ISO-8601 UTC (e.g. '2026-09-24T10:00:00Z').
            until: window end, ISO-8601 UTC.
            paths: optional repo-relative paths; only commits touching them are returned,
                e.g. ['services/payment-service'].
            grep: optional case-insensitive text to match in commit messages (literal, not regex).
            limit: max commits to return (capped by the server).
        """

        async def run() -> dict[str, Any]:
            root = validate_repo(repo, settings.repos)
            window = validate_time_range(since, until, settings.max_range_days)
            clean_paths = validate_paths(paths, root)
            pattern = validate_grep(grep)
            size = clamp(limit, settings.max_commits)
            args = [
                "log",
                f"--since={window.git_since}",
                f"--until={window.git_until}",
                f"--max-count={size + 1}",
                "--no-renames",
                "--name-status",
                f"--format={RECORD}{COMMIT_FORMAT}",
            ]
            if pattern:
                args += [f"--grep={pattern}", "--regexp-ignore-case", "--fixed-strings"]
            args += ["--end-of-options", "HEAD", "--", *clean_paths]
            out = await git.run(root, args)
            commits = parse_log(out, await tag_index(root))
            for commit in commits:
                commit["files"] = commit["files"][: settings.max_files]
            return {
                "repo": repo,
                "since": window.since.isoformat(),
                "until": window.until.isoformat(),
                "paths": clean_paths,
                "commits": commits[:size],
                "truncated": len(commits) > size,
            }

        return await guarded(run())

    @server.tool()
    async def get_commit(repo: str, sha: str) -> dict[str, Any]:
        """Commit metadata, full message, tags and per-file line stats (no diff).

        Args:
            repo: repository name.
            sha: commit SHA (full or abbreviated) or tag name.
        """

        async def run() -> dict[str, Any]:
            root = validate_repo(repo, settings.repos)
            full = await resolve_commit(root, sha)
            meta = await git.run(
                root, ["show", "--no-patch", f"--format={COMMIT_FORMAT}{FIELD}%b", full]
            )
            sha_, parents, author, email, date, subject, body = meta.split(FIELD, 6)
            numstat = await git.run(root, ["show", "--format=", "--numstat", "--no-renames", full])
            files = []
            for line in numstat.splitlines():
                added, deleted, path = line.split("\t", 2)
                files.append(
                    {
                        "path": path,
                        "added": int(added) if added.isdigit() else None,
                        "deleted": int(deleted) if deleted.isdigit() else None,
                    }
                )
            return {
                "repo": repo,
                "sha": sha_,
                "parents": parents.split(),
                "author": author,
                "email": email,
                "date": date,
                "subject": subject,
                "body": body.strip(),
                "tags": (await tag_index(root)).get(sha_, []),
                "files": files[: settings.max_files],
                "truncated": len(files) > settings.max_files,
            }

        return await guarded(run())

    @server.tool()
    async def get_diff(
        repo: str,
        sha: str | None = None,
        base: str | None = None,
        head: str | None = None,
        paths: list[str] | None = None,
        context_lines: int = 3,
    ) -> dict[str, Any]:
        """Unified diff split into files and hunks.

        Either `sha` (the changes introduced by one commit) or `base` + `head` (e.g. two
        release tags 'payment-service/v1.8.1' and 'payment-service/v1.8.2').

        Args:
            repo: repository name.
            sha: commit SHA or tag.
            base: range start (commit or tag), used with head.
            head: range end (commit or tag), used with base.
            paths: optional repo-relative paths to limit the diff to.
            context_lines: unchanged lines around each change (0-10).
        """

        async def run() -> dict[str, Any]:
            root = validate_repo(repo, settings.repos)
            clean_paths = validate_paths(paths, root)
            context = clamp(context_lines, MAX_CONTEXT_LINES, 0)
            common = [
                "--no-color",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                f"--unified={context}",
            ]
            if sha and not (base or head):
                full = await resolve_commit(root, sha)
                args = ["show", "--format=", "--patch", *common, full]
                target: dict[str, str] = {"sha": full}
            elif base and head and not sha:
                b = await resolve_commit(root, base, "base")
                h = await resolve_commit(root, head, "head")
                args = ["diff", *common, b, h]
                target = {"base": b, "head": h}
            else:
                raise GuardError("pass either sha, or both base and head")
            out = await git.run(root, [*args, "--", *clean_paths])
            files, truncated = cap_diff(
                parse_diff(out), settings.max_diff_chars, settings.max_files
            )
            return {
                "repo": repo,
                **target,
                "paths": clean_paths,
                "files": [f.as_dict() for f in files],
                "truncated": truncated,
            }

        return await guarded(run())

    return server
