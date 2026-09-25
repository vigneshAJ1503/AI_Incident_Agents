"""Agent registry: the orchestrator discovers agents and their capabilities here."""

from __future__ import annotations

from aiops.agents.base import AgentSpec, BaseAgent
from aiops.core.config import ConfigError


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, type[BaseAgent]] = {}

    def register[A: type[BaseAgent]](self, agent_cls: A) -> A:
        name = agent_cls.spec.name
        if name in self._agents and self._agents[name] is not agent_cls:
            raise ValueError(f"Agent '{name}' is already registered")
        self._agents[name] = agent_cls
        return agent_cls

    def get(self, name: str) -> type[BaseAgent]:
        try:
            return self._agents[name]
        except KeyError:
            raise ConfigError(f"Unknown agent '{name}' (known: {sorted(self._agents)})") from None

    def specs(self) -> list[AgentSpec]:
        return [cls.spec for _, cls in sorted(self._agents.items())]

    def names(self) -> list[str]:
        return sorted(self._agents)


#: Default registry; agent modules register themselves on import (see aiops.agents).
AGENTS = AgentRegistry()
