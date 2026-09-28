"""``alerts/alertmanager``: alertmanager-mcp tools, Alertmanager matcher syntax for links."""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar
from urllib.parse import quote

from aiops.providers.alerts import AlertsProvider
from aiops.providers.base import ToolRequest
from aiops.providers.registry import PROVIDER_REGISTRY

LIST_ALERTS = "list_alerts"
LIST_SILENCES = "list_silences"


def matcher_filter(labels: Mapping[str, str]) -> str:
    """Alertmanager UI filter syntax: {service="payment-service",namespace="prod"}."""
    inner = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
    return "{" + inner + "}"


class AlertmanagerAlerts(AlertsProvider):
    name: ClassVar[str] = "alertmanager"
    mcp: ClassVar[str] = "mcp-servers/alertmanager-mcp"
    agent_tools: ClassVar[tuple[str, ...]] = (LIST_ALERTS, LIST_SILENCES)
    prompt_fragment: ClassVar[str | None] = "providers/alerts/alertmanager"
    has_history: ClassVar[bool] = False
    history_note: ClassVar[str | None] = (
        "Alertmanager keeps no history: alerts that already resolved are not visible here "
        "(full alert history arrives with Prometheus ALERTS)."
    )

    def alerts_for(self, labels: Mapping[str, str], limit: int) -> ToolRequest:
        return ToolRequest(LIST_ALERTS, {"labels": labels, "state": "all", "limit": limit})

    def silences_for(self, labels: Mapping[str, str], limit: int) -> ToolRequest:
        return ToolRequest(LIST_SILENCES, {"labels": labels, "state": "active", "limit": limit})

    def ui_link(self, template: str | None, labels: Mapping[str, str]) -> str | None:
        if not template:
            return None
        return template.format(filter=quote(matcher_filter(labels), safe=""))


PROVIDER_REGISTRY.register(AlertmanagerAlerts)
