"""Per-capability circuit breakers for MCP servers (PR-042).

A data source that is down should cost one timeout, not one per agent and tool call:

    closed --N consecutive failures--> open --reset_s elapsed--> half-open (ONE probe)
      ^                                  ^                           |
      +------------- probe succeeds -----+---- probe fails ----------+

While open, ``MCPRegistry.toolset()`` refuses at once with ``CircuitOpenError`` (an
``MCPClientError``): the agent reports "Data source unavailable", the orchestrator records
the gap and the investigation becomes PARTIAL with the missing source named in the report.
Failures = connect errors, timeouts and transport errors; a tool-level error (bad query)
is the caller's problem, not the server's, and counts as a success.

Breakers live for the process (``BREAKERS``), shared by every agent and investigation,
keyed by capability + server target. Replays never use one.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Literal

from aiops.core.config import CapabilityLimits
from aiops.mcp.client import MCPClientError

State = Literal["closed", "open", "half_open"]


class CircuitOpenError(MCPClientError):
    """The capability's circuit is open: the call was not attempted."""


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        reset_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_s = reset_s
        self._clock = clock
        self._lock = threading.Lock()
        self._state: State = "closed"
        self.failures = 0
        self._opened_at = 0.0
        self._probing = False
        self.last_error: str | None = None

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def retry_in(self) -> float:
        return max(0.0, self._opened_at + self.reset_s - self._clock())

    def allow(self) -> bool:
        """May a call go through now? In half-open state only one probe at a time."""
        with self._lock:
            if self._state == "closed":
                return True
            if self._state == "open":
                if self._clock() - self._opened_at < self.reset_s:
                    return False
                self._state = "half_open"
                self._probing = False
            if self._probing:
                return False
            self._probing = True
            return True

    def check(self) -> None:
        """``allow()`` or raise ``CircuitOpenError`` with a readable reason."""
        if not self.allow():
            raise CircuitOpenError(
                f"circuit open for '{self.name}' after {self.failures} consecutive failures "
                f"(last: {self.last_error or 'unknown'}); not called, next probe in "
                f"{self.retry_in():.0f}s"
            )

    def record_success(self) -> None:
        with self._lock:
            self._state = "closed"
            self.failures = 0
            self._probing = False
            self.last_error = None

    def record_failure(self, error: str = "") -> None:
        with self._lock:
            self.failures += 1
            self.last_error = error[:200] or self.last_error
            self._probing = False
            if self._state == "half_open" or self.failures >= self.failure_threshold:
                self._state = "open"
                self._opened_at = self._clock()


class BreakerBoard:
    """Process-wide breakers, one per (capability, server target)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, capability: str, target: str, limits: CapabilityLimits) -> CircuitBreaker:
        key = f"{capability}|{target}"
        with self._lock:
            breaker = self._breakers.get(key)
            if breaker is None:
                breaker = CircuitBreaker(
                    capability,
                    failure_threshold=limits.breaker_failures,
                    reset_s=limits.breaker_reset_s,
                )
                self._breakers[key] = breaker
            return breaker

    def states(self) -> dict[str, State]:
        with self._lock:
            return {key: b.state for key, b in self._breakers.items()}

    def reset(self) -> None:
        with self._lock:
            self._breakers.clear()


BREAKERS = BreakerBoard()
