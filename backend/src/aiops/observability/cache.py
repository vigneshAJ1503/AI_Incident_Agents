"""Short-TTL cache for identical read-only tool calls within ONE investigation (PR-041).

Agents of the same investigation often ask the same question twice (a round-2 follow-up
repeats a round-1 query, two agents list the same alerts). The second call is answered
from here instead of hitting the backend again.

Safety rules:
* only agent toolsets use it: they are read-only by construction (write tools are never
  given to agents; the approval executor's write toolset never caches);
* one cache per investigation (the orchestrator owns it and drops it at the end), and
  keys include the investigation id too: nothing is shared across investigations;
* only successful results are cached, for ``orchestrator.tool_cache_ttl_s`` (default
  120 s), with an LRU bound on the number of entries;
* a cached answer is still a recorded, audited ``ToolCall`` (``cached: true``) and still
  counts against the agent's ``max_tool_calls``.
"""

from __future__ import annotations

import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aiops.mcp.toolset import ToolOutcome

CacheKey = tuple[str, str, str, str]


def cache_key(
    investigation_id: str, capability: str, tool: str, arguments: dict[str, Any]
) -> CacheKey:
    return (
        investigation_id,
        capability,
        tool,
        json.dumps(arguments, sort_keys=True, default=str, separators=(",", ":")),
    )


class ToolCallCache:
    def __init__(
        self,
        ttl_s: float,
        max_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[CacheKey, tuple[float, ToolOutcome]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    @property
    def enabled(self) -> bool:
        return self.ttl_s > 0

    def get(self, key: CacheKey) -> ToolOutcome | None:
        if not self.enabled:
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or self._clock() - entry[0] > self.ttl_s:
                if entry is not None:
                    del self._entries[key]
                self.misses += 1
                return None
            self._entries.move_to_end(key)
            self.hits += 1
            return entry[1]

    def put(self, key: CacheKey, outcome: ToolOutcome) -> None:
        if not self.enabled or not outcome.ok:
            return
        with self._lock:
            self._entries[key] = (self._clock(), outcome)
            self._entries.move_to_end(key)
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def __len__(self) -> int:
        return len(self._entries)
