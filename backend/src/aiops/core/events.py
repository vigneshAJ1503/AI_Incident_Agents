"""Investigation events (streamed to the UI via SSE later, PR-035)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aiops.core.models import utcnow

EventType = Literal[
    "agent_started",
    "llm_called",
    "tool_called",
    "agent_finished",
]


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: EventType
    agent: str
    task_id: str
    investigation_id: str | None = None
    timestamp: datetime = Field(default_factory=utcnow)
    data: dict[str, Any] = Field(default_factory=dict)


class EventSink(Protocol):
    def emit(self, event: Event) -> None: ...


class NullEventSink:
    def emit(self, event: Event) -> None:
        return None


class MemoryEventSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


class CallbackEventSink:
    def __init__(self, callback: Callable[[Event], None]) -> None:
        self._callback = callback

    def emit(self, event: Event) -> None:
        self._callback(event)
