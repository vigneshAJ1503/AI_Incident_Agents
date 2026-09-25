"""Requires the git-mcp container (docker compose ... up -d --build git-mcp).

Rebuilds .data/sample-repo (S1 at the fixture time; git-mcp mounts it read-only) and
runs the Code agent against the live server. Run with: make test-integration
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aiops.agents.code_agent import CodeAgent
from aiops.agents.deps import build_deps
from aiops.core.config import load_settings
from aiops.core.guardrails.audit import MemoryAuditSink
from aiops.core.models import AgentStatus
from aiops.evals.replay import echo_responder
from aiops.llm.fake import FakeLLMProvider
from aiops.mcp.registry import MCPRegistry
from aiops.seed.git_repo import build_sample_repo, default_repo_path
from tests.fixtures.scenario_context import FIXED_NOW, task_for

pytestmark = pytest.mark.integration
CONFIG = Path(__file__).resolve().parents[3] / "config"


async def test_code_capability_guardrails_live() -> None:
    build_sample_repo(default_repo_path(), "S1", FIXED_NOW)
    settings = load_settings("local", CONFIG)
    audit = MemoryAuditSink()
    async with MCPRegistry(settings, audit=audit).toolset("code", agent="it") as tools:
        names = {s.name for s in await tools.specs()}
        assert names == {
            "list_repositories",
            "list_releases",
            "search_commits",
            "get_commit",
            "get_diff",
        }
        ok = await tools.call(
            "get_commit", {"repo": "sample-repo", "sha": "payment-service/v1.8.2"}
        )
        assert ok.ok and ok.data["subject"] == "release payment-service v1.8.2"
        traversal = await tools.call(
            "get_diff", {"repo": "sample-repo", "sha": "HEAD", "paths": ["../../etc"]}
        )
        assert not traversal.ok and "leaves the repository" in traversal.content
        other = await tools.call("list_releases", {"repo": "/etc"})
        assert not other.ok and "not allowed" in other.content


def test_code_agent_s1_live() -> None:
    build_sample_repo(default_repo_path(), "S1", FIXED_NOW)
    settings = load_settings("local", CONFIG)
    deps = build_deps(settings, llm=FakeLLMProvider(responder=echo_responder()))
    result = asyncio.run(CodeAgent(deps).run(task_for("S1", "code")))
    assert result.status is AgentStatus.SUCCESS
    assert "risky_config_change" in result.signals
    top = max(
        (e for e in result.evidence if e.source == "code.get_diff"), key=lambda e: e.data["risk"]
    )
    assert top.data["commit"]["subject"] == "tune db pool"
    assert '+  DB_POOL_SIZE: "2"' in top.data["hunks"][0]["hunk"]
