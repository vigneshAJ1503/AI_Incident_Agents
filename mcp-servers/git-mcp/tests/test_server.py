from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp import Client

from git_mcp.config import ServerSettings
from git_mcp.server import create_server

SINCE, UNTIL = "2026-09-24T10:00:00Z", "2026-09-25T10:30:00Z"


async def call(settings: ServerSettings, tool: str, args: dict[str, Any]) -> Any:
    async with Client(create_server(settings)) as client:
        return await client.call_tool(tool, args)


async def ok(settings: ServerSettings, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    result = await call(settings, tool, args)
    assert not result.is_error, result.content[0].text
    data = result.structured_content
    assert isinstance(data, dict)
    return data


async def test_tools_listed(settings: ServerSettings) -> None:
    async with Client(create_server(settings)) as client:
        names = {t.name for t in (await client.list_tools()).tools}
    assert names == {
        "list_repositories",
        "list_releases",
        "search_commits",
        "get_commit",
        "get_diff",
    }


async def test_list_repositories(settings: ServerSettings) -> None:
    assert (await ok(settings, "list_repositories", {}))["repositories"] == ["sample-repo"]


async def test_list_releases_newest_first_with_commit(
    settings: ServerSettings, repo: dict[str, str]
) -> None:
    data = await ok(settings, "list_releases", {"repo": "sample-repo"})
    assert [r["tag"] for r in data["releases"]] == [
        "payment-service/v1.8.2",
        "payment-service/v1.8.1",
    ]
    newest = data["releases"][0]
    assert newest["sha"] == repo["pool"]  # peeled annotated tag -> commit
    assert newest["date"].startswith("2026-09-25T09:55:00")
    filtered = await ok(
        settings, "list_releases", {"repo": "sample-repo", "prefix": "order-service/"}
    )
    assert filtered["releases"] == []


async def test_search_commits_window_and_paths(
    settings: ServerSettings, repo: dict[str, str]
) -> None:
    data = await ok(
        settings,
        "search_commits",
        {
            "repo": "sample-repo",
            "since": SINCE,
            "until": UNTIL,
            "paths": ["services/payment-service"],
        },
    )
    assert [c["subject"] for c in data["commits"]] == ["tune db pool"]
    pool = data["commits"][0]
    assert pool["sha"] == repo["pool"] and pool["author"] == "Dev"
    assert pool["date"] == "2026-09-25T08:20:00Z"
    assert pool["tags"] == ["payment-service/v1.8.2"]
    assert pool["files"] == [
        {"status": "modified", "path": "services/payment-service/config/app.yaml"}
    ]

    everything = await ok(
        settings, "search_commits", {"repo": "sample-repo", "since": SINCE, "until": UNTIL}
    )
    assert [c["subject"] for c in everything["commits"]] == [
        "order-service: refactor",
        "tune db pool",
        "docs: readme",
    ]  # the initial commit is before `since`
    assert everything["truncated"] is False


async def test_search_commits_grep_and_limit(settings: ServerSettings) -> None:
    data = await ok(
        settings,
        "search_commits",
        {"repo": "sample-repo", "since": "2026-09-20T00:00:00Z", "until": UNTIL, "grep": "DB POOL"},
    )
    assert [c["subject"] for c in data["commits"]] == ["tune db pool"]
    capped = await ok(
        settings,
        "search_commits",
        {"repo": "sample-repo", "since": "2026-09-20T00:00:00Z", "until": UNTIL, "limit": 2},
    )
    assert len(capped["commits"]) == 2 and capped["truncated"] is True


async def test_get_commit(settings: ServerSettings, repo: dict[str, str]) -> None:
    data = await ok(settings, "get_commit", {"repo": "sample-repo", "sha": repo["pool"][:8]})
    assert data["sha"] == repo["pool"]
    assert data["subject"] == "tune db pool"
    assert data["tags"] == ["payment-service/v1.8.2"]
    assert data["files"] == [
        {"path": "services/payment-service/config/app.yaml", "added": 1, "deleted": 1}
    ]
    by_tag = await ok(
        settings, "get_commit", {"repo": "sample-repo", "sha": "payment-service/v1.8.2"}
    )
    assert by_tag["sha"] == repo["pool"]


async def test_get_diff_for_commit_and_range(
    settings: ServerSettings, repo: dict[str, str]
) -> None:
    data = await ok(settings, "get_diff", {"repo": "sample-repo", "sha": repo["pool"]})
    (only,) = data["files"]
    assert only["path"] == "services/payment-service/config/app.yaml"
    assert only["hunks"][0].startswith("@@")
    assert '-  DB_POOL_SIZE: "20"' in only["hunks"][0]
    assert '+  DB_POOL_SIZE: "2"' in only["hunks"][0]

    ranged = await ok(
        settings,
        "get_diff",
        {
            "repo": "sample-repo",
            "base": "payment-service/v1.8.1",
            "head": "payment-service/v1.8.2",
            "paths": ["services/payment-service"],
        },
    )
    assert [f["path"] for f in ranged["files"]] == ["services/payment-service/config/app.yaml"]


async def test_get_diff_is_capped(repo: dict[str, str]) -> None:
    tiny = ServerSettings(repos={"sample-repo": Path(repo["root"])}, max_diff_chars=100)
    data = await ok(tiny, "get_diff", {"repo": "sample-repo", "sha": repo["orders"]})
    assert data["truncated"] is True
    assert data["files"][0]["hunks"] == []


async def test_guard_violations_are_tool_errors(
    settings: ServerSettings, repo: dict[str, str]
) -> None:
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("list_releases", {"repo": "other"}, "not allowed"),
        ("list_releases", {"repo": "../sample-repo"}, "not allowed"),
        (
            "search_commits",
            {"repo": "sample-repo", "since": SINCE, "until": UNTIL, "paths": ["../etc"]},
            "leaves the repository",
        ),
        (
            "search_commits",
            {"repo": "sample-repo", "since": SINCE, "until": UNTIL, "paths": ["/etc/passwd"]},
            "not allowed",
        ),
        (
            "search_commits",
            {"repo": "sample-repo", "since": SINCE, "until": UNTIL, "paths": [":(top)x"]},
            "not allowed",
        ),
        (
            "search_commits",
            {"repo": "sample-repo", "since": SINCE, "until": UNTIL, "paths": [".git/config"]},
            "leaves the repository",
        ),
        (
            "search_commits",
            {"repo": "sample-repo", "since": "2025-01-01T00:00:00Z", "until": UNTIL},
            "maximum",
        ),
        (
            "search_commits",
            {"repo": "sample-repo", "since": "yesterday", "until": UNTIL},
            "ISO-8601",
        ),
        ("get_commit", {"repo": "sample-repo", "sha": "--output=/tmp/pwned"}, "not a valid"),
        ("get_commit", {"repo": "sample-repo", "sha": "HEAD~1"}, "not a valid"),
        ("get_commit", {"repo": "sample-repo", "sha": "main..HEAD"}, "not a valid"),
        ("get_commit", {"repo": "sample-repo", "sha": "deadbeef"}, "does not name a commit"),
        ("get_diff", {"repo": "sample-repo"}, "either sha"),
        (
            "get_diff",
            {"repo": "sample-repo", "sha": repo["pool"], "base": repo["init"]},
            "either sha",
        ),
    ]
    for tool, args, message in cases:
        result = await call(settings, tool, args)
        assert result.is_error, (tool, args)
        assert message in result.content[0].text, (tool, args, result.content[0].text)
