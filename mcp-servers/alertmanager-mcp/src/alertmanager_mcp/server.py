"""MCP tools: list_alerts, get_alert, list_silences, get_alert_groups, alert_history (read-only)."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Awaitable
from datetime import UTC, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from alertmanager_mcp.am import AlertmanagerClient, AlertmanagerError
from alertmanager_mcp.config import ServerSettings
from alertmanager_mcp.guards import (
    AlertState,
    GuardError,
    SilenceState,
    build_matchers,
    clamp_limit,
    merge_filters,
    state_flags,
    validate_fingerprint,
    validate_time_range,
)
from alertmanager_mcp.prom import PrometheusClient, PrometheusError, firing_intervals

INSTRUCTIONS = """Read-only access to Alertmanager: firing alerts, alert groups and silences.
Filter by labels (equality only), e.g. service='payment-service'. Alerts are symptoms
reported by monitoring rules: evidence to correlate with other data, not a root cause.
Alertmanager keeps no history of resolved alerts; alert_history reads Prometheus's ALERTS
series when Prometheus is configured, and says what it covered ('complete', 'sources')."""

HISTORY_NOTE = (
    "Alertmanager keeps no alert history: its API returns only alerts that have not ended "
    "(resolved alerts are dropped). These are the alerts still firing that overlap the "
    "requested range. Full history (resolved alerts) needs the Prometheus ALERTS series: "
    "set PROMETHEUS_URL on this server."
)
PROMETHEUS_HISTORY_NOTE = (
    "Firing intervals from Prometheus's ALERTS series (resolved alerts included), plus "
    "alerts known only to Alertmanager (posted by another source, e.g. seeded alerts). "
    "Times are accurate to one step."
)
PROMETHEUS_DOWN_NOTE = (
    "Prometheus could not be queried ({error}); showing only the alerts Alertmanager still "
    "holds (no resolved alerts)."
)

SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def compact_alert(alert: dict[str, Any]) -> dict[str, Any]:
    """The fields an investigator needs. endsAt is omitted: for alerts that haven't
    ended it is only an expiry time, easily misread as a resolution time."""
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    status = alert.get("status", {})
    return {
        "fingerprint": alert.get("fingerprint"),
        "alertname": labels.get("alertname"),
        "service": labels.get("service"),
        "severity": labels.get("severity"),
        "state": status.get("state"),  # active | suppressed | unprocessed
        "starts_at": alert.get("startsAt"),
        "summary": annotations.get("summary"),
        "description": annotations.get("description"),
        "runbook_url": annotations.get("runbook_url") or annotations.get("runbook"),
        "labels": labels,
        "silenced_by": status.get("silencedBy") or [],
        "inhibited_by": status.get("inhibitedBy") or [],
        "generator_url": alert.get("generatorURL"),
    }


def _sort_key(alert: dict[str, Any]) -> tuple[int, str, str]:
    rank = SEVERITY_RANK.get(str(alert.get("severity")), len(SEVERITY_RANK))
    return rank, str(alert.get("starts_at") or ""), str(alert.get("alertname") or "")


def _matcher_text(matcher: dict[str, Any]) -> str:
    op = {
        (True, False): "=",
        (False, False): "!=",
        (True, True): "=~",
        (False, True): "!~",
    }[(matcher.get("isEqual", True), matcher.get("isRegex", False))]
    return f'{matcher.get("name")}{op}"{matcher.get("value")}"'


def _matcher_allows(matcher: dict[str, Any], value: str) -> bool:
    if matcher.get("isRegex"):
        try:
            hit = re.fullmatch(str(matcher.get("value", "")), value) is not None
        except re.error:
            return True  # can't evaluate: keep the silence visible
    else:
        hit = matcher.get("value") == value
    return hit if matcher.get("isEqual", True) else not hit


def silence_could_apply(silence: dict[str, Any], labels: dict[str, str]) -> bool:
    """True unless one of the silence's matchers contradicts a requested label.
    Matchers on other labels can't be judged without an alert, so they don't exclude."""
    for matcher in silence.get("matchers", []):
        name = matcher.get("name")
        if name in labels and not _matcher_allows(matcher, labels[name]):
            return False
    return True


def _label_key(labels: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def create_server(
    settings: ServerSettings,
    am: AlertmanagerClient | None = None,
    prometheus: PrometheusClient | None = None,
) -> MCPServer:
    client = am or AlertmanagerClient(settings)
    prom = prometheus or (PrometheusClient(settings) if settings.prometheus_url else None)
    server = MCPServer("alertmanager-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    async def guarded[T](coro: Awaitable[T]) -> T:
        try:
            return await coro
        except AlertmanagerError as exc:
            raise ToolError(str(exc)) from exc

    def matchers_for(
        labels: dict[str, str] | None,
        service: str | None,
        severity: str | None,
        alertname: str | None,
    ) -> list[str]:
        try:
            merged = merge_filters(labels, service=service, severity=severity, alertname=alertname)
            return build_matchers(merged, settings.max_filters)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc

    async def fetch_alerts(matchers: list[str], state: str) -> list[dict[str, Any]]:
        try:
            flags = state_flags(state)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        raw = await guarded(
            client.alerts(
                matchers, active=flags.active, silenced=flags.silenced, inhibited=flags.inhibited
            )
        )
        return sorted((compact_alert(a) for a in raw), key=_sort_key)

    def limited(limit: int) -> int:
        try:
            return clamp_limit(limit, settings.max_results)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc

    def page(items: list[dict[str, Any]], limit: int) -> dict[str, Any]:
        shown = items[:limit]
        return {"total": len(items), "returned": len(shown), "truncated": len(items) > len(shown)}

    @server.tool()
    async def list_alerts(
        service: str | None = None,
        severity: str | None = None,
        alertname: str | None = None,
        labels: dict[str, str] | None = None,
        state: AlertState = "active",
        limit: int = 50,
    ) -> dict[str, Any]:
        """List alerts currently known to Alertmanager, most severe first.

        Args:
            service: filter on the 'service' label, e.g. 'payment-service'.
            severity: filter on the 'severity' label, e.g. 'critical' or 'warning'.
            alertname: filter on the alert name, e.g. 'HighErrorRate'.
            labels: more equality filters, e.g. {"namespace": "prod"}.
            state: 'active' (firing, not muted), 'suppressed' (silenced or inhibited) or 'all'.
            limit: max alerts to return (capped by the server).
        """
        matchers = matchers_for(labels, service, severity, alertname)
        cap = limited(limit)
        alerts = await fetch_alerts(matchers, state)
        return {
            "state": state,
            "filters": matchers,
            **page(alerts, cap),
            "by_severity": dict(Counter(str(a["severity"]) for a in alerts)),
            "alerts": alerts[:cap],
        }

    @server.tool()
    async def get_alert(fingerprint: str) -> dict[str, Any]:
        """Full details of one alert by its fingerprint (from list_alerts)."""
        try:
            wanted = validate_fingerprint(fingerprint)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        for alert in await fetch_alerts([], "all"):
            if alert["fingerprint"] == wanted:
                return {"alert": alert}
        raise ToolError(
            f"No alert with fingerprint '{wanted}'. It may have resolved: Alertmanager only "
            "keeps alerts that have not ended."
        )

    @server.tool()
    async def list_silences(
        service: str | None = None,
        labels: dict[str, str] | None = None,
        state: SilenceState = "active",
        limit: int = 50,
    ) -> dict[str, Any]:
        """List silences (muted alerts) that could apply to the given labels.

        Args:
            service: only silences that could mute alerts of this service.
            labels: more labels to check against the silences' matchers, e.g. {"alertname": "HighErrorRate"}.
            state: 'active', 'pending' (starts later), 'expired' or 'all'.
            limit: max silences to return (capped by the server).
        """
        if state not in ("active", "pending", "expired", "all"):
            raise ToolError(f"state must be one of active, pending, expired, all (got '{state}')")
        try:
            wanted = merge_filters(labels, service=service)
            build_matchers(wanted, settings.max_filters)  # same validation as alert filters
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        cap = limited(limit)
        raw = await guarded(client.silences())
        silences = [
            {
                "id": s.get("id"),
                "state": s.get("status", {}).get("state"),
                "matchers": [_matcher_text(m) for m in s.get("matchers", [])],
                "starts_at": s.get("startsAt"),
                "ends_at": s.get("endsAt"),
                "created_by": s.get("createdBy"),
                "comment": s.get("comment"),
            }
            for s in raw
            if (state == "all" or s.get("status", {}).get("state") == state)
            and silence_could_apply(s, wanted)
        ]
        silences.sort(key=lambda s: (str(s["state"]), str(s["ends_at"])))
        return {"state": state, **page(silences, cap), "silences": silences[:cap]}

    @server.tool()
    async def get_alert_groups(
        service: str | None = None,
        severity: str | None = None,
        alertname: str | None = None,
        labels: dict[str, str] | None = None,
        state: AlertState = "active",
        limit: int = 50,
    ) -> dict[str, Any]:
        """Alerts grouped the way Alertmanager notifies (by service and alert name here).

        Args:
            service / severity / alertname / labels: equality filters, as in list_alerts.
            state: 'active', 'suppressed' or 'all'.
            limit: max alerts returned across all groups (capped by the server).
        """
        matchers = matchers_for(labels, service, severity, alertname)
        cap = limited(limit)
        try:
            flags = state_flags(state)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        raw = await guarded(
            client.alert_groups(
                matchers, active=flags.active, silenced=flags.silenced, inhibited=flags.inhibited
            )
        )
        groups = [
            {
                "labels": group.get("labels", {}),
                "receiver": group.get("receiver", {}).get("name"),
                "alerts": sorted(
                    (compact_alert(a) for a in group.get("alerts", [])), key=_sort_key
                ),
            }
            for group in raw
        ]
        # Most severe group first; the alert budget is spent in that order.
        groups.sort(key=lambda g: _sort_key(g["alerts"][0]) if g["alerts"] else (99, "", ""))
        budget, total = cap, 0
        for group in groups:
            count = len(group["alerts"])
            total += count
            group["alert_count"] = count
            group["alerts"] = group["alerts"][: max(budget, 0)]
            budget -= count
        return {
            "state": state,
            "filters": matchers,
            "group_count": len(groups),
            "total_alerts": total,
            "truncated": total > cap,
            "groups": groups,
        }

    @server.tool()
    async def alert_history(
        start: str,
        end: str,
        service: str | None = None,
        severity: str | None = None,
        alertname: str | None = None,
        labels: dict[str, str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Alerts that were firing at some point in [start, end] (ISO-8601 UTC), oldest first.

        With Prometheus configured, includes alerts that already resolved (firing_since /
        resolved_at); otherwise only still-firing alerts: see 'complete' and 'note'.
        Args:
            start: window start, e.g. '2026-09-25T10:00:00Z'.
            end: window end.
            service / severity / alertname / labels: equality filters, as in list_alerts.
            limit: max alerts to return (capped by the server).
        """
        try:
            begin, finish = validate_time_range(start, end, settings.max_time_range_hours)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        matchers = matchers_for(labels, service, severity, alertname)
        cap = limited(limit)
        alerts = [
            a
            for a in await fetch_alerts(matchers, "all")
            if (starts := _ts(a["starts_at"])) is not None and starts <= finish
        ]
        alerts.sort(key=lambda a: str(a["starts_at"]))
        time_range = {"start": begin.isoformat(), "end": finish.isoformat()}
        note = HISTORY_NOTE
        if prom is not None:
            try:
                series, step = await prom.firing_series(matchers, begin, finish)
            except PrometheusError as exc:
                note = PROMETHEUS_DOWN_NOTE.format(error=exc)
            else:
                history = firing_intervals(series, step, begin, finish)
                # Annotations (summary, runbook) live in Alertmanager, not in ALERTS.
                current = {_label_key(a["labels"]): a for a in alerts}
                for entry in history:
                    match = current.get(_label_key(entry["labels"]))
                    if match is not None:
                        entry.update(
                            fingerprint=match["fingerprint"],
                            summary=match["summary"],
                            runbook_url=match["runbook_url"],
                            starts_at=match["starts_at"],
                        )
                known = {_label_key(e["labels"]) for e in history}
                extra = [
                    {**a, "source": "alertmanager"}
                    for a in alerts
                    if _label_key(a["labels"]) not in known
                ]
                merged = history + extra
                return {
                    "sources": ["prometheus", "alertmanager"],
                    "complete": True,
                    "note": PROMETHEUS_HISTORY_NOTE,
                    "step_s": step,
                    "time_range": time_range,
                    "filters": matchers,
                    **page(merged, cap),
                    "alerts": merged[:cap],
                }
        return {
            "sources": ["alertmanager"],
            "complete": False,
            "note": note,
            "time_range": time_range,
            "filters": matchers,
            **page(alerts, cap),
            "alerts": alerts[:cap],
        }

    return server
