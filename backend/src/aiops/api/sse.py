"""SSE for ``GET /investigations/{id}/events`` (docs/api/contract.md "Live events").

1. Replay: stored events after ``Last-Event-ID`` (header, or ``?last_event_id=``).
2. Follow: when the investigation runs in this process, the live ``EventBus`` (subscribed
   BEFORE reading history, de-duplicated by ``seq``, so nothing is lost in between);
   otherwise the store is polled (another API process may be running it).
3. A ``heartbeat`` every ``api.heartbeat_s`` while nothing happens; heartbeats carry the
   ``seq`` of the last event sent and are never stored.
4. The stream ends after ``investigation_finished``.

Framing: ``id: <seq>``, ``event: <type>`` and ``data: <the JSON envelope>`` per event.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from aiops.core.events import EventBus, InvestigationEvent
from aiops.core.models import InvestigationStatus, utcnow
from aiops.store.repository import InvestigationStore

FINISHED = "investigation_finished"
#: Polling interval when following an investigation run by another process.
STORE_POLL_S = 1.0
OPEN_STATUSES = {InvestigationStatus.PENDING, InvestigationStatus.RUNNING}


def frame(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"), default=str)
    return f"id: {payload['seq']}\nevent: {payload['type']}\ndata: {data}\n\n"


def event_frame(event: InvestigationEvent) -> str:
    return frame(event.model_dump(mode="json"))


def heartbeat_frame(investigation_id: str, seq: int) -> str:
    return frame(
        {
            "type": "heartbeat",
            "investigation_id": investigation_id,
            "timestamp": utcnow().isoformat(),
            "seq": seq,
            "agent": None,
            "data": {},
        }
    )


async def event_stream(
    investigation_id: str,
    *,
    after: int,
    store: InvestigationStore,
    bus: EventBus,
    is_live: Callable[[str], bool],
    heartbeat_s: float,
    poll_s: float = STORE_POLL_S,
) -> AsyncIterator[str]:
    queue: asyncio.Queue[InvestigationEvent] = asyncio.Queue()
    unsubscribe = bus.subscribe(queue.put_nowait, investigation_id)
    last = after
    try:
        live = is_live(investigation_id)
        # 1. replay (the store, then this process's history for what isn't flushed yet)
        backlog = await store.events(investigation_id, after)
        if live:
            backlog += bus.history(investigation_id, after)
        for event in sorted(backlog, key=lambda e: e.seq):
            if event.seq <= last:
                continue
            last = event.seq
            yield event_frame(event)
            if event.type == FINISHED:
                return
        # 2. follow
        if live or is_live(investigation_id):
            while True:
                try:
                    async with asyncio.timeout(heartbeat_s):
                        event = await queue.get()
                except TimeoutError:
                    yield heartbeat_frame(investigation_id, last)
                    continue
                if event.seq <= last:
                    continue
                last = event.seq
                yield event_frame(event)
                if event.type == FINISHED:
                    return
        # Not running here: follow the store while the investigation is still open.
        waited = 0.0
        while True:
            inv = await store.get(investigation_id)
            if inv is None or inv.status not in OPEN_STATUSES:
                # Finished without a stored finish event (e.g. demo data): catch up and end.
                for event in await store.events(investigation_id, last):
                    last = event.seq
                    yield event_frame(event)
                return
            await asyncio.sleep(poll_s)
            waited += poll_s
            for event in await store.events(investigation_id, last):
                last = event.seq
                yield event_frame(event)
                if event.type == FINISHED:
                    return
            if waited >= heartbeat_s:
                waited = 0.0
                yield heartbeat_frame(investigation_id, last)
    finally:
        unsubscribe()
