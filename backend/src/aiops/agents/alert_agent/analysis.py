"""Deterministic alert analysis: which alerts fire, where, and when relative to the incident.

The LLM reasons over these results; it doesn't have to parse timestamps, compare
them with the incident window or decide what counts as "firing".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from aiops.core.models import TimeRange

#: Signal vocabulary (must match config/prompts/alerts/v*.md). All are data-derived.
SIGNALS = (
    "alerts_firing",
    "critical_alert_firing",
    "dependency_alert_firing",
    "alert_precedes_incident",
    "no_active_alerts",
)

NO_ALERTS_PHRASE = "No active alerts"
ROOT_CAUSE_NOTE = (
    "Alerts are symptoms reported by monitoring rules: correlate them with the incident, "
    "never present an alert as the root cause."
)
HISTORY_NOTE = (
    "Alertmanager keeps no history: alerts that already resolved are not visible here "
    "(full alert history arrives with Prometheus ALERTS)."
)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def minutes(delta_s: float) -> str:
    sign = "+" if delta_s >= 0 else "-"
    return f"{sign}{abs(delta_s) / 60:.0f}m"


@dataclass(frozen=True)
class AlertView:
    alertname: str
    service: str
    severity: str
    state: str  # active | suppressed | unprocessed
    starts_at: datetime | None
    summary: str
    runbook_url: str | None
    fingerprint: str | None
    scope: str  # the service itself, or the dependency name it was queried for
    is_dependency: bool

    @property
    def suppressed(self) -> bool:
        return self.state == "suppressed"

    def line(self, incident_start: datetime, window: TimeRange) -> str:
        when = "start unknown"
        if self.starts_at is not None:
            offset = minutes((self.starts_at - incident_start).total_seconds())
            where = (
                "before the window"
                if self.starts_at < window.start
                else "after the window"
                if self.starts_at > window.end
                else "inside the window"
            )
            when = f"since {iso(self.starts_at)} ({offset} vs incident start, {where})"
        runbook = f" | runbook: {self.runbook_url}" if self.runbook_url else ""
        return f"  - [{self.severity}] {self.alertname} on {self.service} {when} | {self.summary}{runbook}"


def alert_views(data: Any, scope: str, *, is_dependency: bool) -> list[AlertView]:
    """AlertViews from a list_alerts tool result (alertmanager-mcp's compact format)."""
    alerts = data.get("alerts", []) if isinstance(data, dict) else []
    views = []
    for a in alerts:
        if not isinstance(a, dict):
            continue
        labels = a.get("labels") or {}
        views.append(
            AlertView(
                alertname=str(a.get("alertname") or labels.get("alertname") or "unknown"),
                service=str(a.get("service") or labels.get("service") or scope),
                severity=str(a.get("severity") or labels.get("severity") or "unknown"),
                state=str(a.get("state") or "active"),
                starts_at=parse_ts(a.get("starts_at")),
                summary=str(a.get("summary") or ""),
                runbook_url=a.get("runbook_url"),
                fingerprint=a.get("fingerprint"),
                scope=scope,
                is_dependency=is_dependency,
            )
        )
    return views


@dataclass
class AlertAnalysis:
    service: str
    dependencies: list[str]
    window: TimeRange
    incident_start: datetime
    incident_start_source: str  # "window start" or "hint"
    critical_severities: frozenset[str]
    alerts: list[AlertView] = field(default_factory=list)
    silences: list[dict[str, Any]] = field(default_factory=list)
    failed_scopes: list[str] = field(default_factory=list)

    # -- classification ------------------------------------------------------------------

    def _started_by_window_end(self, a: AlertView) -> bool:
        return a.starts_at is None or a.starts_at <= self.window.end

    @property
    def firing(self) -> list[AlertView]:
        """Active (not silenced/inhibited) alerts that had started by the end of the window."""
        return [a for a in self.alerts if not a.suppressed and self._started_by_window_end(a)]

    @property
    def suppressed(self) -> list[AlertView]:
        return [a for a in self.alerts if a.suppressed and self._started_by_window_end(a)]

    @property
    def later(self) -> list[AlertView]:
        return [a for a in self.alerts if not self._started_by_window_end(a)]

    def is_critical(self, a: AlertView) -> bool:
        return a.severity.casefold() in self.critical_severities

    @property
    def signals(self) -> list[str]:
        firing = self.firing
        found = set()
        if any(not a.is_dependency for a in firing):
            found.add("alerts_firing")
        if any(a.is_dependency for a in firing):
            found.add("dependency_alert_firing")
        if any(self.is_critical(a) for a in firing):
            found.add("critical_alert_firing")
        if any(a.starts_at is not None and a.starts_at < self.incident_start for a in firing):
            found.add("alert_precedes_incident")
        if not firing and not self.failed_scopes:
            found.add("no_active_alerts")
        return [s for s in SIGNALS if s in found]

    # -- text ----------------------------------------------------------------------------

    def no_alerts_sentence(self) -> str:
        deps = f" or its dependencies ({', '.join(self.dependencies)})" if self.dependencies else ""
        return f"{NO_ALERTS_PHRASE} for {self.service}{deps}: no alert threshold was breached."

    def lines(self) -> list[str]:
        """Compact text for the LLM prompt."""
        w = self.window
        out = [
            f"Incident window: {iso(w.start)} to {iso(w.end)}. Incident start used for "
            f"correlation: {iso(self.incident_start)} ({self.incident_start_source}).",
        ]
        own = [a for a in self.firing if not a.is_dependency]
        deps = [a for a in self.firing if a.is_dependency]
        if not self.firing and not self.failed_scopes:
            out.append(self.no_alerts_sentence())
        if own:
            out.append(f"Firing alerts on {self.service} ({len(own)}):")
            out.extend(a.line(self.incident_start, w) for a in own)
        elif self.firing:
            out.append(f"No firing alerts on {self.service} itself.")
        if deps:
            out.append(f"Firing alerts on dependencies ({len(deps)}):")
            out.extend(a.line(self.incident_start, w) for a in deps)
        elif self.dependencies:
            out.append(f"Dependencies checked, none firing: {', '.join(self.dependencies)}.")
        if self.firing:
            first = min(
                (a for a in self.firing if a.starts_at is not None),
                key=lambda a: a.starts_at or w.end,
                default=None,
            )
            if first is not None and first.starts_at is not None:
                out.append(
                    f"First alert to fire: {first.alertname} on {first.service} at "
                    f"{iso(first.starts_at)}."
                )
        if self.suppressed:
            out.append(f"Suppressed (silenced or inhibited) alerts ({len(self.suppressed)}):")
            out.extend(a.line(self.incident_start, w) for a in self.suppressed)
        if self.silences:
            out.append(f"Active silences that could apply ({len(self.silences)}):")
            out.extend(
                f"  - {s.get('id')}: {', '.join(s.get('matchers', []))} until {s.get('ends_at')} "
                f"({s.get('comment') or 'no comment'})"
                for s in self.silences
            )
        if self.later:
            out.append(
                "Alerts that started after the window (not part of this incident window): "
                + ", ".join(f"{a.alertname} on {a.service}" for a in self.later)
            )
        if self.failed_scopes:
            out.append(f"Could not query alerts for: {', '.join(self.failed_scopes)}.")
        out.append(HISTORY_NOTE)
        out.append(ROOT_CAUSE_NOTE)
        out.append(f"Deterministic signals: {', '.join(self.signals) or 'none'}")
        return out


def analyze(
    service_data: Any,
    dependency_data: dict[str, Any],
    silences_data: Any,
    *,
    service: str,
    window: TimeRange,
    incident_start: datetime,
    incident_start_source: str,
    critical_severities: list[str],
    failed_scopes: list[str] | None = None,
) -> AlertAnalysis:
    alerts = alert_views(service_data, service, is_dependency=False)
    for dep, data in dependency_data.items():
        alerts.extend(alert_views(data, dep, is_dependency=True))
    silences = silences_data.get("silences", []) if isinstance(silences_data, dict) else []
    return AlertAnalysis(
        service=service,
        dependencies=list(dependency_data),
        window=window,
        incident_start=incident_start,
        incident_start_source=incident_start_source,
        critical_severities=frozenset(s.casefold() for s in critical_severities),
        alerts=alerts,
        silences=[s for s in silences if isinstance(s, dict)],
        failed_scopes=list(failed_scopes or []),
    )
