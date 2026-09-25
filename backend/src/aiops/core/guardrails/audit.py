"""Audit trail of every tool call (who, what, arguments, outcome, duration).

JSONL for now; the evidence store (PR-032) adds a Postgres sink.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from aiops.core.models import ToolCall, new_id, utcnow


class AuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: new_id("audit"))
    recorded_at: Any = Field(default_factory=utcnow)
    request_id: str
    investigation_id: str | None = None
    tool_call: ToolCall  # arguments are already redacted
    redactions: dict[str, int] = Field(default_factory=dict)


class AuditSink(Protocol):
    def record(self, record: AuditRecord) -> None: ...


class MemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def record(self, record: AuditRecord) -> None:
        self.records.append(record)


class JsonlAuditSink:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def record(self, record: AuditRecord) -> None:
        line = json.dumps(record.model_dump(mode="json"), sort_keys=True)
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as handle:
                handle.write(line + "\n")
