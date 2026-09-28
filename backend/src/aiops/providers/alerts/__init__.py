"""The ``alerts`` capability: what the Alert agent asks, and the neutral alert shapes.

Neutral result shapes (alertmanager-mcp already returns them; other providers map to them):

* alerts: ``{"alerts": [{alertname, service, severity, state (active|suppressed|
  unprocessed), starts_at (ISO), summary, runbook_url, fingerprint, labels}]}``
* silences: ``{"silences": [{id, matchers: [str], ends_at, comment}]}``
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

from aiops.providers.base import Provider, ToolRequest


class AlertsProvider(Provider):
    """Interface of an ``alerts`` provider (override every ``NotImplementedError``)."""

    capability: ClassVar[str] = "alerts"
    #: Does the provider return alerts that already resolved? If not, ``history_note``
    #: is added to the overview so the LLM doesn't read absence as "nothing happened".
    has_history: ClassVar[bool] = True
    history_note: ClassVar[str | None] = None

    def alerts_for(self, labels: Mapping[str, str], limit: int) -> ToolRequest:
        """Alerts in any state whose labels match ``labels`` (service identifiers)."""
        raise NotImplementedError

    def silences_for(self, labels: Mapping[str, str], limit: int) -> ToolRequest:
        """Active silences / maintenance windows that could apply to ``labels``."""
        raise NotImplementedError

    def alerts(self, data: Any) -> Any:
        """Normalize an alerts result into the neutral shape (identity by default)."""
        return data

    def silences(self, data: Any) -> Any:
        """Normalize a silences result into the neutral shape (identity by default)."""
        return data

    def ui_link(self, template: str | None, labels: Mapping[str, str]) -> str | None:
        """Deep link to the alerting UI for ``labels`` (``settings.ui_link_template``)."""
        raise NotImplementedError
