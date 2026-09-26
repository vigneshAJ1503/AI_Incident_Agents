"""MCP tools: query, query_range, list_metrics, metric_metadata, get_targets (read-only)."""

from __future__ import annotations

import math
import time as _time
from collections.abc import Awaitable
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from prometheus_mcp.config import ServerSettings
from prometheus_mcp.guards import (
    GuardError,
    check_query,
    iso,
    metric_allowed,
    parse_time,
    validate_limit,
    validate_metric_name,
    validate_range,
)
from prometheus_mcp.prom import PrometheusClient, PrometheusError

INSTRUCTIONS = """Read-only access to Prometheus metrics (PromQL).
query = instant value(s) at one time; query_range = time series over [start, end] with a
step (auto-chosen when omitted). Every selector must name its metric. Time ranges, steps,
points and series are capped by the server; results report 'truncated' when capped.
Use list_metrics / metric_metadata to discover names, get_targets to check scrape health."""

TargetState = Literal["active", "dropped", "any"]


def number(raw: Any) -> float | None:
    """Prometheus sample value (a string) -> float; NaN/Inf -> None (not JSON-safe)."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return float(f"{value:.6g}")


def _labels_key(series: dict[str, Any]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(series.get("metric", {}).items()))


def shape_result(data: Any, max_series: int) -> dict[str, Any]:
    """Compact, deterministic result: sorted series, rounded values, capped series count."""
    if not isinstance(data, dict):
        return {"result_type": None, "series_total": 0, "returned": 0, "truncated": False}
    kind = data.get("resultType")
    result = data.get("result")
    if kind in ("scalar", "string"):
        ts, raw = result if isinstance(result, list) and len(result) == 2 else (None, None)
        value = number(raw) if kind == "scalar" else raw
        return {"result_type": kind, "time": iso(float(ts)) if ts else None, "value": value}
    series_in = sorted(result or [], key=_labels_key)
    shown = series_in[:max_series]
    series: list[dict[str, Any]] = []
    for s in shown:
        item: dict[str, Any] = {"labels": s.get("metric", {})}
        if kind == "matrix":
            item["values"] = [[int(float(t)), number(v)] for t, v in s.get("values", [])]
        else:
            ts, raw = s.get("value", [None, None])
            item["value"] = number(raw)
            item["time"] = iso(float(ts)) if ts is not None else None
        series.append(item)
    return {
        "result_type": kind,
        "series_total": len(series_in),
        "returned": len(series),
        "truncated": len(series_in) > len(series),
        "series": series,
    }


def compact_target(t: dict[str, Any]) -> dict[str, Any]:
    labels = t.get("labels", {})
    return {
        "job": labels.get("job") or t.get("scrapePool"),
        "instance": labels.get("instance"),
        "health": t.get("health"),
        "last_error": t.get("lastError") or None,
        "last_scrape": t.get("lastScrape"),
        "last_scrape_duration_s": number(t.get("lastScrapeDuration")),
        "scrape_url": t.get("scrapeUrl"),
        "labels": labels,
    }


def create_server(settings: ServerSettings, prom: PrometheusClient | None = None) -> MCPServer:
    client = prom or PrometheusClient(settings)
    server = MCPServer("prometheus-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    async def guarded[T](coro: Awaitable[T]) -> T:
        try:
            return await coro
        except PrometheusError as exc:
            raise ToolError(str(exc)) from exc

    def checked(query: str) -> list[str]:
        try:
            return check_query(query, settings)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc

    @server.tool()
    async def query(query: str, time: str | None = None) -> dict[str, Any]:
        """Evaluate a PromQL expression at one instant (default: now).

        Args:
            query: PromQL, e.g. 'sum by (service) (rate(http_requests_total[5m]))'.
            time: ISO-8601 UTC (e.g. '2026-09-25T10:30:00Z') or unix seconds; default now.
        """
        metrics = checked(query)
        try:
            at = parse_time(time, "time") if time else None
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        data = await guarded(client.query(query, at))
        return {
            "query": query,
            "time": iso(at if at is not None else _time.time()),
            "metrics": metrics,
            **shape_result(data, settings.max_series),
        }

    @server.tool()
    async def query_range(
        query: str, start: str, end: str, step: str | None = None
    ) -> dict[str, Any]:
        """Evaluate a PromQL expression over [start, end] at a fixed step.

        Args:
            query: PromQL, e.g. 'histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket{service="x"}[5m])))'.
            start: ISO-8601 UTC or unix seconds.
            end: ISO-8601 UTC or unix seconds (range capped by the server).
            step: e.g. '30s', '1m' or seconds; omitted = the finest step within the point cap.
        Values are [unix_seconds, value] pairs; null = NaN/Inf (e.g. 0/0).
        """
        metrics = checked(query)
        try:
            window = validate_range(start, end, step, settings)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        data = await guarded(client.query_range(query, window.start, window.end, window.step))
        return {
            "query": query,
            "start": iso(window.start),
            "end": iso(window.end),
            "step_s": window.step,
            "metrics": metrics,
            **shape_result(data, settings.max_series),
        }

    @server.tool()
    async def list_metrics(match: str | None = None, limit: int = 100) -> dict[str, Any]:
        """Metric names Prometheus knows (allowlisted ones only).

        Args:
            match: case-insensitive substring filter, e.g. 'http_' or 'pool'.
            limit: max names returned (capped by the server).
        """
        try:
            cap = validate_limit(limit, settings.max_results)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        needle = (match or "").casefold()
        names = sorted(
            n
            for n in await guarded(client.metric_names())
            if needle in n.casefold() and metric_allowed(n, settings.metric_allowlist)
        )
        return {
            "match": match,
            "total": len(names),
            "returned": min(cap, len(names)),
            "truncated": len(names) > cap,
            "metrics": names[:cap],
        }

    @server.tool()
    async def metric_metadata(metric: str | None = None, limit: int = 50) -> dict[str, Any]:
        """Type (counter/gauge/histogram), help text and unit of metrics.

        Args:
            metric: one metric name, e.g. 'http_request_duration_seconds'; omitted = all.
            limit: max metrics returned (capped by the server).
        """
        try:
            name = validate_metric_name(metric) if metric else None
            cap = validate_limit(limit, settings.max_results)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        if name and not metric_allowed(name, settings.metric_allowlist):
            raise ToolError(f"metric '{name}' is not allowed by the server allowlist")
        raw = await guarded(client.metadata(name, cap))
        items = [
            {
                "metric": n,
                "type": entries[0].get("type") if entries else None,
                "help": entries[0].get("help") if entries else None,
                "unit": (entries[0].get("unit") or None) if entries else None,
            }
            for n, entries in sorted(raw.items())
            if metric_allowed(n, settings.metric_allowlist)
        ]
        return {"total": len(items), "metadata": items[:cap], "truncated": len(items) > cap}

    @server.tool()
    async def get_targets(state: TargetState = "active", limit: int = 100) -> dict[str, Any]:
        """Scrape targets and their health (is the data even being collected?).

        Args:
            state: 'active', 'dropped' or 'any'.
            limit: max targets returned (capped by the server).
        """
        if state not in ("active", "dropped", "any"):
            raise ToolError(f"state must be one of active, dropped, any (got '{state}')")
        try:
            cap = validate_limit(limit, settings.max_results)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        raw = await guarded(client.targets(state))
        active = [compact_target(t) for t in raw.get("activeTargets", [])]
        active.sort(key=lambda t: (str(t["job"]), str(t["instance"])))
        dropped = len(raw.get("droppedTargets", []))
        down = [t for t in active if t["health"] != "up"]
        return {
            "state": state,
            "total": len(active),
            "down": len(down),
            "dropped": dropped,
            "truncated": len(active) > cap,
            "targets": active[:cap],
        }

    return server
