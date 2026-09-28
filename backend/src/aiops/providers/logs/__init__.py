"""The ``logs`` capability: what the Log agent asks, and the neutral shapes it gets back.

Every provider (``elasticsearch`` today; Loki, OpenSearch, Splunk, ... later) implements
``LogsProvider``. Results are normalized into ``LogTable`` (columns + rows) with these
neutral column names:

* ``volume_by_level``: ``window`` (current|baseline), ``level``, ``count``
* ``message_patterns``: ``window``, ``level``, ``msg``, ``count``, ``first_seen``, ``last_seen``
* ``versions_and_startups``: ``window``, ``version``, ``count``, ``starts``, ``first_seen``
* ``first_occurrences``: ``timestamp``, ``trace_id`` (and ``version`` when mapped)

``window`` is ``"current"`` for rows at/after the incident start, ``"baseline"`` before.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, ClassVar

from aiops.providers.base import Provider, ToolRequest


@dataclass(frozen=True)
class LogScope:
    """Where and how to look (vendor-neutral: values come from settings and the catalog)."""

    index: str  # the index / stream / source selector from the catalog
    fields: dict[str, str]  # role -> field name (timestamp, level, message, service, ...)
    error_levels: list[str]
    pattern_levels: list[str]
    baseline: timedelta
    startup_pattern: str  # glob ('Starting*') matched against the message
    link_template: str | None
    #: Service value to filter on when several services share one index (None = no filter).
    service_value: str | None = None


@dataclass(frozen=True)
class LogWindow:
    """The incident window; queries cover ``[start - baseline, end]``."""

    start: datetime
    end: datetime
    baseline: timedelta

    @property
    def scope_start(self) -> datetime:
        return self.start - self.baseline


@dataclass(frozen=True)
class LogTable:
    """A normalized tabular result (neutral column names, see the module docstring)."""

    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"columns": self.columns, "rows": self.rows}


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


def literal_prefix(template_text: str, min_len: int = 8) -> str | None:
    """Literal text before the first placeholder of a message template (``None`` when
    too short to be distinctive): the phrase a provider searches for."""
    prefix = template_text.split("<", 1)[0].rstrip()
    if len(prefix) < min_len:
        return None
    return prefix


class LogsProvider(Provider):
    """Interface of a ``logs`` provider (override every method that raises
    ``NotImplementedError``). Queries must return the neutral column names."""

    capability: ClassVar[str] = "logs"
    #: Field-role defaults of this vendor's conventions (overridden by ``settings.fields``).
    default_fields: ClassVar[Mapping[str, str]] = {}

    def fields(self) -> dict[str, str]:
        return {**self.default_fields, **self.settings.get("fields", {})}

    # -- the questions the Log agent asks ---------------------------------------------------

    def volume_by_level(self, scope: LogScope, window: LogWindow) -> ToolRequest:
        """Line counts by window and level."""
        raise NotImplementedError

    def message_patterns(self, scope: LogScope, window: LogWindow, limit: int) -> ToolRequest:
        """Counts + first/last seen of ``pattern_levels`` messages by window, level, message."""
        raise NotImplementedError

    def versions_and_startups(self, scope: LogScope, window: LogWindow) -> ToolRequest:
        """Lines, startup lines (``startup_pattern``) and first seen by window and version."""
        raise NotImplementedError

    def first_occurrences(
        self, scope: LogScope, window: LogWindow, phrase: str, limit: int
    ) -> ToolRequest:
        """The first lines (incident window) whose message starts with ``phrase``."""
        raise NotImplementedError

    def phrase(self, template_text: str) -> str | None:
        """The literal phrase ``first_occurrences`` can search for (None = skip)."""
        return literal_prefix(template_text)

    def table(self, request: ToolRequest, data: Any) -> LogTable | None:
        """Normalize a tool result into a ``LogTable`` (renaming ``request.columns``);
        None when the result isn't tabular."""
        if not isinstance(data, dict):
            return None
        columns = [request.columns.get(c, c) for c in data.get("columns", [])]
        return LogTable(columns, list(data.get("rows", [])))

    # -- links and prompt ---------------------------------------------------------------

    def ui_link(
        self,
        scope: LogScope,
        window: LogWindow,
        *,
        levels: Sequence[str] | None = None,
        phrase: str | None = None,
    ) -> str | None:
        """Deep link to the log UI for the scope, optionally narrowed to levels or a phrase."""
        raise NotImplementedError

    def scope_note(self, scope: LogScope) -> str:
        """Extra prompt text about the scope (e.g. how to filter a shared index)."""
        return ""
