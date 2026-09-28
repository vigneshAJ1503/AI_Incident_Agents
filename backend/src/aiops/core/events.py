"""Events.

* ``Event``: low-level agent events (agent_started, llm_called, tool_called,
  agent_finished) emitted by ``BaseAgent`` into an ``EventSink``.
* ``InvestigationEvent``: the SSE events of docs/api/contract.md, published by the
  orchestrator on an ``EventBus`` (the API streams them, the store persists them).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any, Literal, Protocol, get_args

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


# --------------------------------------------------------------------------- investigation


#: SSE event types of docs/api/contract.md ("Live events"). Nothing else is ever emitted.
InvestigationEventType = Literal[
    "investigation_started",
    "clarification_needed",
    "plan_created",
    "round_started",
    "agent_started",
    "tool_called",
    "evidence_added",
    "agent_finished",
    "rca_started",
    "hypothesis_ranked",
    "report_ready",
    "approval_requested",
    "investigation_finished",
    "error",
    "heartbeat",
]
INVESTIGATION_EVENT_TYPES: tuple[str, ...] = get_args(InvestigationEventType)


class InvestigationEvent(BaseModel):
    """One contract event: ``{type, investigation_id, timestamp, seq, agent, data}``."""

    model_config = ConfigDict(extra="forbid")

    type: InvestigationEventType
    investigation_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    seq: int = Field(ge=1)
    agent: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


InvestigationListener = Callable[[InvestigationEvent], None]


class EventBus:
    """In-process pub/sub of investigation events (the API's SSE endpoint subscribes).

    Keeps every event per investigation (numbered ``seq`` 1, 2, ...) so a late
    subscriber, or a reconnect with ``Last-Event-ID``, replays history first.
    Listeners are sync callables (e.g. a persistence sink or ``queue.put_nowait``).
    """

    def __init__(self) -> None:
        self._history: dict[str, list[InvestigationEvent]] = {}
        self._listeners: dict[str | None, list[InvestigationListener]] = {}

    def publish(
        self,
        type_: InvestigationEventType,
        investigation_id: str,
        *,
        agent: str | None = None,
        timestamp: datetime | None = None,
        **data: Any,
    ) -> InvestigationEvent:
        history = self._history.setdefault(investigation_id, [])
        event = InvestigationEvent(
            type=type_,
            investigation_id=investigation_id,
            timestamp=timestamp or utcnow(),
            seq=len(history) + 1,
            agent=agent,
            data=data,
        )
        history.append(event)
        listeners = [*self._listeners.get(investigation_id, []), *self._listeners.get(None, [])]
        for listener in listeners:
            listener(event)
        return event

    def history(self, investigation_id: str, after_seq: int = 0) -> list[InvestigationEvent]:
        return [e for e in self._history.get(investigation_id, []) if e.seq > after_seq]

    def subscribe(
        self, listener: InvestigationListener, investigation_id: str | None = None
    ) -> Callable[[], None]:
        """Listen to one investigation (or all when ``None``); returns an unsubscribe."""
        listeners = self._listeners.setdefault(investigation_id, [])
        listeners.append(listener)

        def unsubscribe() -> None:
            if listener in listeners:
                listeners.remove(listener)

        return unsubscribe

    def restore(self, investigation_id: str, events: list[InvestigationEvent]) -> None:
        """Seed the history of an investigation (e.g. from the store after a restart) so
        new events continue its ``seq`` numbering (a clarification resumes a stream)."""
        if investigation_id not in self._history:
            self._history[investigation_id] = sorted(events, key=lambda e: e.seq)

    def forget(self, investigation_id: str) -> None:
        self._history.pop(investigation_id, None)
        self._listeners.pop(investigation_id, None)
