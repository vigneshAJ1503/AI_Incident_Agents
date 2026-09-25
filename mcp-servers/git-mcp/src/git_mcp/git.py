"""Minimal, read-only git runner and output parsers.

* Fixed argv via ``asyncio.create_subprocess_exec`` (never a shell).
* Only read-only subcommands are allowed (``READ_ONLY``).
* Hardened config: user/system config ignored, no hooks, fsmonitor, pager,
  external diff or textconv (a hostile ``.git/config`` can't run programs);
  literal pathspecs; no optional locks (nothing is written to the repo).
* Output is capped and every command has a timeout.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

READ_ONLY = frozenset({"log", "show", "diff", "rev-parse", "for-each-ref"})
MAX_OUTPUT_BYTES = 4_000_000
FIELD, RECORD = "\x1f", "\x1e"


class GitError(Exception):
    """git failed or timed out. The message is safe to show to the model."""


def _env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GIT_LITERAL_PATHSPECS="1",
        GIT_NO_REPLACE_OBJECTS="1",
        LC_ALL="C",
        TZ="UTC",
    )
    return env


class GitRunner:
    def __init__(self, binary: str = "git", timeout_s: float = 15.0) -> None:
        self.binary = binary
        self.timeout_s = timeout_s
        self._env = _env()

    def argv(self, root: Path, args: list[str]) -> list[str]:
        if not args or args[0] not in READ_ONLY:
            raise GitError(f"git subcommand not allowed: {args[:1]}")
        return [
            self.binary,
            "-c",
            f"safe.directory={root}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.pager=cat",
            "-c",
            "diff.external=",
            "--no-pager",
            "-C",
            str(root),
            *args,
        ]

    async def run(self, root: Path, args: list[str], *, ok_codes: tuple[int, ...] = (0,)) -> str:
        argv = self.argv(root, args)
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env,
            )
        except FileNotFoundError as exc:
            raise GitError("git is not installed on the MCP server") from exc
        try:
            async with asyncio.timeout(self.timeout_s):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise GitError(f"git {args[0]} timed out after {self.timeout_s:g}s") from None
        if proc.returncode not in ok_codes:
            message = stderr.decode("utf-8", "replace").strip().splitlines()
            raise GitError(f"git {args[0]} failed: {message[-1] if message else proc.returncode}")
        return stdout[:MAX_OUTPUT_BYTES].decode("utf-8", "replace")


# --------------------------------------------------------------------------- parsers

COMMIT_FORMAT = FIELD.join(["%H", "%P", "%an", "%ae", "%cI", "%s"])
STATUS = {"A": "added", "M": "modified", "D": "deleted", "T": "type_changed"}


def parse_log(output: str, tags: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Parse ``git log --format=<RS>COMMIT_FORMAT --name-status`` output."""
    commits: list[dict[str, Any]] = []
    for record in output.split(RECORD):
        if not record.strip():
            continue
        header, _, rest = record.partition("\n")
        sha, parents, author, email, date, subject = header.split(FIELD)
        files = []
        for line in rest.splitlines():
            status, _, path = line.partition("\t")
            if path:
                files.append({"status": STATUS.get(status[:1], status), "path": path})
        commits.append(
            {
                "sha": sha,
                "short_sha": sha[:10],
                "parents": parents.split(),
                "author": author,
                "email": email,
                "date": date,
                "subject": subject,
                "tags": tags.get(sha, []),
                "files": files,
            }
        )
    return commits


@dataclass
class FileDiff:
    path: str
    status: str = "modified"
    hunks: list[str] = field(default_factory=list)
    binary: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "status": self.status,
            "hunks": self.hunks,
            "binary": self.binary,
        }


_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.+) b/(?P<b>.+)$")


def parse_diff(output: str) -> list[FileDiff]:
    """Split a unified diff into files and hunks (each hunk keeps its ``@@`` header)."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    hunk: list[str] | None = None

    def flush() -> None:
        nonlocal hunk
        if current is not None and hunk:
            current.hunks.append("\n".join(hunk))
        hunk = None

    for line in output.splitlines():
        match = _DIFF_HEADER.match(line)
        if match:
            flush()
            current = FileDiff(path=match.group("b"))
            files.append(current)
            continue
        if current is None:
            continue
        if hunk is None:
            if line.startswith("new file mode"):
                current.status = "added"
            elif line.startswith("deleted file mode"):
                current.status = "deleted"
            elif line.startswith("Binary files"):
                current.binary = True
            elif line.startswith("@@"):
                hunk = [line]
            continue
        if line.startswith("@@"):
            flush()
            hunk = [line]
        else:
            hunk.append(line)
    flush()
    return files


def cap_diff(files: list[FileDiff], max_chars: int, max_files: int) -> tuple[list[FileDiff], bool]:
    """Keep whole hunks until ``max_chars``; report whether anything was dropped."""
    kept: list[FileDiff] = []
    used = 0
    truncated = len(files) > max_files
    for f in files[:max_files]:
        out = FileDiff(path=f.path, status=f.status, binary=f.binary)
        for hunk in f.hunks:
            if used + len(hunk) > max_chars:
                truncated = True
                break
            out.hunks.append(hunk)
            used += len(hunk)
        kept.append(out)
    return kept, truncated
