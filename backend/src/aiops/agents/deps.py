"""Wire up agent dependencies from settings."""

from __future__ import annotations

from pathlib import Path

from aiops.agents.base import AgentDeps
from aiops.core.catalog import ServiceCatalog
from aiops.core.config import Settings
from aiops.core.events import EventSink, NullEventSink
from aiops.core.prompts import PromptLoader
from aiops.llm.base import LLMProvider
from aiops.llm.factory import create_provider
from aiops.mcp.registry import MCPRegistry


def build_deps(
    settings: Settings,
    *,
    llm: LLMProvider | None = None,
    mcp: MCPRegistry | None = None,
    events: EventSink | None = None,
    record_dir: Path | None = None,
    replay_dir: Path | None = None,
) -> AgentDeps:
    return AgentDeps(
        settings=settings,
        llm=llm or create_provider(settings.llm),
        mcp=mcp or MCPRegistry(settings, record_dir=record_dir, replay_dir=replay_dir),
        prompts=PromptLoader(settings.config_dir / "prompts"),
        catalog=ServiceCatalog.from_settings(settings),
        events=events or NullEventSink(),
    )
