"""Provider adapters: the vendor side of a capability (ADR-0012).

Agents say *what* they need ("error volume by level in window W vs baseline B", "open
tickets for these components"); a provider, chosen by ``capabilities.<cap>.provider`` in
the profile, turns that into the vendor's query language and MCP tool calls, and
normalizes the results into the capability's provider-neutral shapes.

Moving to a company with another vendor = choosing (or writing) a provider + config;
agent code never changes. See docs/portability.md, "How to add a provider".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar


def iso(ts: datetime) -> str:
    """UTC timestamps as tools expect them: ``2026-09-25T10:00:00Z``."""
    return ts.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ToolRequest:
    """One MCP tool call a provider wants the agent to make (tool name + arguments).

    ``columns`` optionally maps vendor result columns to the capability's neutral column
    names (applied by the capability's normalize method)."""

    tool: str
    arguments: dict[str, Any]
    columns: Mapping[str, str] = field(default_factory=dict)


class Provider:
    """Base class of every provider adapter.

    Class attributes describe the provider for the registry and ``aiops profile
    validate`` (they replace the hand-written rows of the provider matrix):

    * ``capability`` / ``name``: the registry key, e.g. ``("logs", "elasticsearch")``;
    * ``mcp``: the MCP server that implements the tool contract;
    * ``required``: dotted ``capabilities.<cap>.settings`` keys that must be set;
    * ``agent_tools``: tools the agent calls itself (must be in ``tool_allowlist``);
    * ``prompt_fragment``: ``config/prompts/<fragment>/vN.md`` with the vendor's query
      guidance, rendered into the agent prompt as ``$provider_guidance``.
    """

    capability: ClassVar[str]
    name: ClassVar[str]
    mcp: ClassVar[str]
    required: ClassVar[tuple[str, ...]] = ()
    agent_tools: ClassVar[tuple[str, ...]] = ()
    note: ClassVar[str] = ""
    prompt_fragment: ClassVar[str | None] = None

    def __init__(self, settings: Mapping[str, Any] | None = None) -> None:
        #: ``capabilities.<cap>.settings`` of the active profile.
        self.settings: dict[str, Any] = dict(settings or {})

    @classmethod
    def key(cls) -> tuple[str, str]:
        return cls.capability, cls.name
