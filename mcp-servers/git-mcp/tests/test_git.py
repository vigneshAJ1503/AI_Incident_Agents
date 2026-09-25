from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mcp import Client

from git_mcp.config import ServerSettings, parse_repos
from git_mcp.git import FileDiff, GitError, GitRunner, cap_diff, parse_diff
from git_mcp.server import create_server
from tests.conftest import commit, git

DIFF = """\
diff --git a/a.yaml b/a.yaml
index 1..2 100644
--- a/a.yaml
+++ b/a.yaml
@@ -1,2 +1,2 @@
 env:
-  X: "1"
+  X: "2"
@@ -10 +10 @@ tail
-y
+z
diff --git a/new.sql b/new.sql
new file mode 100644
--- /dev/null
+++ b/new.sql
@@ -0,0 +1 @@
+DROP INDEX i;
"""


def test_parse_diff_files_and_hunks() -> None:
    files = parse_diff(DIFF)
    assert [(f.path, f.status, len(f.hunks)) for f in files] == [
        ("a.yaml", "modified", 2),
        ("new.sql", "added", 1),
    ]
    assert files[0].hunks[0] == '@@ -1,2 +1,2 @@\n env:\n-  X: "1"\n+  X: "2"'


def test_cap_diff_keeps_whole_hunks() -> None:
    files = [FileDiff("a", hunks=["x" * 50, "y" * 50]), FileDiff("b", hunks=["z" * 10])]
    kept, truncated = cap_diff(files, max_chars=60, max_files=5)
    assert truncated and kept[0].hunks == ["x" * 50] and kept[1].hunks == ["z" * 10]
    kept, truncated = cap_diff(files, max_chars=1000, max_files=1)
    assert truncated and [f.path for f in kept] == ["a"]


def test_parse_repos() -> None:
    assert parse_repos("a=/r/a, b=/r/b") == {"a": Path("/r/a"), "b": Path("/r/b")}
    with pytest.raises(ValueError, match="name=/abs/path"):
        parse_repos("oops")


async def test_only_read_only_subcommands(tmp_path: Path) -> None:
    runner = GitRunner()
    for sub in ("commit", "push", "reset", "checkout", "config", "gc", "-c"):
        with pytest.raises(GitError, match="not allowed"):
            await runner.run(tmp_path, [sub])


async def test_hostile_repo_config_cannot_run_programs(tmp_path: Path) -> None:
    """A repo's .git/config may set a pager, external diff or fsmonitor: none may execute."""
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    root = tmp_path / "evil"
    root.mkdir()
    git(root, "init", "-q", "--initial-branch=main")
    sha = commit(root, {"a.txt": "1\n"}, "one", "2026-09-25T08:00:00+00:00")
    sha = commit(root, {"a.txt": "2\n"}, "two", "2026-09-25T09:00:00+00:00")
    marker = tmp_path / "pwned"
    script = tmp_path / "evil.sh"
    script.write_text(f"#!/bin/sh\ntouch {marker}\n")
    script.chmod(0o755)
    for key in ("core.pager", "diff.external", "core.fsmonitor", "core.hooksPath"):
        subprocess.run(["git", "-C", str(root), "config", key, str(script)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "diff.default.textconv", str(script)], check=True
    )
    settings = ServerSettings(repos={"evil": root})
    async with Client(create_server(settings)) as client:
        result = await client.call_tool("get_diff", {"repo": "evil", "sha": sha})
        assert not result.is_error, result.content[0].text
        await client.call_tool(
            "search_commits",
            {"repo": "evil", "since": "2026-09-25T00:00:00Z", "until": "2026-09-25T10:00:00Z"},
        )
        await client.call_tool("get_commit", {"repo": "evil", "sha": sha})
    assert not marker.exists()


async def test_timeout_is_reported(tmp_path: Path) -> None:
    runner = GitRunner(binary="git", timeout_s=0.000001)
    with pytest.raises(GitError, match="timed out"):
        await runner.run(tmp_path, ["log"])
