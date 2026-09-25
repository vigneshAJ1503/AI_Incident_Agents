"""Re-record Knowledge agent MCP fixtures from the live local stack.

    make infra-up ingest-knowledge
    docker compose --env-file .env.example -f deploy/compose/docker-compose.mcp.yml up -d --build knowledge-mcp
    make record-knowledge-fixtures     # = cd backend && uv run python -m tests.fixtures.record_knowledge

Each scenario is recorded twice into one fixture file: with the hints from
``scenarios/<id>/agents/knowledge.yaml`` (representative Log agent findings) and
without hints (question only), so replay tests can cover both.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from aiops.agents.deps import build_deps
from aiops.agents.knowledge_agent import KnowledgeAgent
from aiops.core.config import load_settings
from aiops.evals.replay import echo_responder
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.scenario_context import SCENARIOS, task_for

HERE = Path(__file__).parent
SCENARIO_IDS = ["S0", "S1", "S2", "S3", "S4", "S5"]


async def record_variant(scenario: str, hints: bool, out: Path) -> None:
    settings = load_settings("local")
    task = task_for(scenario, "knowledge")
    if not hints:
        task = task.model_copy(update={"hints": {}})
    deps = build_deps(settings, llm=FakeLLMProvider(responder=echo_responder()), record_dir=out)
    result = await KnowledgeAgent(deps).run(task)
    print(scenario, "hints" if hints else "no-hints", result.status.value, result.signals)


def merge(files: list[Path]) -> dict[str, Any]:
    tools: list[Any] = []
    calls: dict[str, Any] = {}
    for file in files:
        payload = json.loads(file.read_text())
        tools = tools or payload["tools"]
        for call in payload["calls"]:
            key = f"{call['tool']}:{json.dumps(call['arguments'], sort_keys=True)}"
            calls.setdefault(key, call)
    return {"tools": tools, "calls": list(calls.values())}


async def record(scenario: str) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for hints in (True, False):
            out = Path(tmp) / ("hints" if hints else "plain")
            await record_variant(scenario, hints, out)
            files.append(out / "knowledge.json")
        target = HERE / "knowledge" / scenario / "knowledge.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(merge(files), indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    assert set(SCENARIO_IDS) <= set(SCENARIOS)
    for s in SCENARIO_IDS:
        asyncio.run(record(s))
