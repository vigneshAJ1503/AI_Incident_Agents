"""Specialized agents. Importing this package registers all built-in agents."""

from aiops.agents import log_agent  # noqa: F401  (registers LogAgent)
from aiops.agents.registry import AGENTS

__all__ = ["AGENTS"]
