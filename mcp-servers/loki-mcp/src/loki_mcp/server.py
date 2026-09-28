"""MCP tools: query_range, query, list_labels, label_values (read-only LogQL)."""

from __future__ import annotations

import math
import time as _time
from collections.abc import Awaitable
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from loki_mcp.config import ServerSettings
from loki_mcp.guards import (
    GuardError,
    check_label,
    check_query,
    default_selector,
    is_log_query,
    iso,
    iso_ns,
    parse_time,
    validate_limit,
    validate_step,
    validate_window,
)
from loki_mcp.loki import LokiClient, LokiError

INSTRUCTIONS = """Read-only access to Grafana Loki logs (LogQL).
query_range = log lines (a log query: '{namespace="prod", app="x"} |= "timeout" | json')
or a metric series over [start, end]; query = one instant metric value per series, e.g.
'sum by (level) (count_over_time({namespace="prod", app="x"} [15m]))'. Every stream
selector must include an allowlisted matcher (list_labels / label_values show what exists).
Time ranges, [ranges], lines, series and points are capped by the server; results report
'truncated' when capped."""

Direction = Literal["backward", "forward"]
DEFAULT_LOOKBACK_S = 3600.0


def number(raw: Any) -> float | int | None:
    """Loki sample value (a string) -> int when whole (exact: counts, epoch ms), else float."""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    if value.is_integer() and abs(value) < 2**53:
        return int(value)
    return float(f"{value:.6g}")


def _labels_key(labels: dict[str, Any]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


def shape_result(data: Any, s: ServerSettings, direction: str = "backward") -> dict[str, Any]:
    """Compact, deterministic result for streams (log lines), vectors and matrices."""
    if not isinstance(data, dict):
        return {"result_type": None, "returned": 0, "truncated": False}
    kind = data.get("resultType")
    result = data.get("result") or []
    if kind == "streams":
        lines: list[tuple[int, dict[str, Any], str]] = []
        for stream in result:
            labels = stream.get("stream", {})
            for ts, line, *_meta in stream.get("values", []):
                lines.append((int(ts), labels, str(line)))
        lines.sort(key=lambda x: x[0], reverse=direction == "backward")
        shown = lines[: s.max_lines]
        return {
            "result_type": "streams",
            "lines_total": len(lines),
            "returned": len(shown),
            "truncated": len(lines) > len(shown),
            "lines": [
                {"timestamp": iso_ns(ts), "labels": labels, "line": line}
                for ts, labels, line in shown
            ],
        }
    if kind in ("scalar", "string"):
        ts, raw = result if isinstance(result, list) and len(result) == 2 else (None, None)
        value = number(raw) if kind == "scalar" else raw
        return {"result_type": kind, "time": iso(float(ts)) if ts else None, "value": value}
    series_in = sorted(result, key=lambda x: _labels_key(x.get("metric", {})))
    shown_series = series_in[: s.max_series]
    series: list[dict[str, Any]] = []
    for item in shown_series:
        out: dict[str, Any] = {"labels": item.get("metric", {})}
        if kind == "matrix":
            out["values"] = [[int(float(t)), number(v)] for t, v in item.get("values", [])]
        else:
            _ts, raw = item.get("value", [None, None])
            out["value"] = number(raw)
        series.append(out)
    return {
        "result_type": kind,
        "series_total": len(series_in),
        "returned": len(series),
        "truncated": len(series_in) > len(series),
        "series": series,
    }


def create_server(settings: ServerSettings, loki: LokiClient | None = None) -> MCPServer:
    client = loki or LokiClient(settings)
    server = MCPServer("loki-mcp", instructions=INSTRUCTIONS, version="0.1.0")

    async def guarded[T](coro: Awaitable[T]) -> T:
        try:
            return await coro
        except LokiError as exc:
            raise ToolError(str(exc)) from exc

    def checked(query: str) -> None:
        try:
            check_query(query, settings)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc

    def window(start: str | None, end: str | None) -> tuple[float, float]:
        try:
            finish = parse_time(end, "end") if end else _time.time()
            begin = parse_time(start, "start") if start else finish - DEFAULT_LOOKBACK_S
            return validate_window(begin, finish, settings)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc

    def scope(selector: str | None) -> str | None:
        if not selector:
            return default_selector(settings)
        checked(selector)
        if not is_log_query(selector):
            raise ToolError('selector must be a stream selector, e.g. {namespace="prod"}')
        return selector

    @server.tool()
    async def query_range(
        query: str,
        start: str,
        end: str,
        limit: int = 100,
        direction: Direction = "backward",
        step: str | None = None,
    ) -> dict[str, Any]:
        """Run LogQL over [start, end]: log lines for a log query, series for a metric query.

        Args:
            query: LogQL, e.g. '{namespace="prod", app="payment-service"} |= "timeout" | json'
                or 'sum by (level) (count_over_time({namespace="prod"} [1m]))'.
            start: ISO-8601 UTC (e.g. '2026-09-25T10:00:00Z') or unix seconds.
            end: ISO-8601 UTC or unix seconds (range capped by the server).
            limit: max log lines (capped by the server); ignored for metric queries.
            direction: 'backward' (newest first) or 'forward' (oldest first).
            step: metric queries only, e.g. '30s'; omitted = the finest within the point cap.
        """
        checked(query)
        begin, finish = window(start, end)
        try:
            cap = validate_limit(limit, settings.max_lines)
            log_query = is_log_query(query)
            step_s = None if log_query else validate_step(begin, finish, step, settings)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        data = await guarded(
            client.query_range(query, begin, finish, limit=cap, direction=direction, step=step_s)
        )
        shaped = shape_result(data, settings, direction)
        if shaped.get("result_type") == "streams":
            # Loki stops at `limit`: a full page means there may be more lines.
            shaped["truncated"] = shaped["truncated"] or shaped["returned"] >= cap
        return {
            "query": query,
            "start": iso(begin),
            "end": iso(finish),
            **({"step_s": step_s} if step_s else {"limit": cap, "direction": direction}),
            **shaped,
        }

    @server.tool()
    async def query(query: str, time: str | None = None) -> dict[str, Any]:
        """Evaluate a metric LogQL query at one instant (default: now): counts, rates, top-k.

        Args:
            query: metric LogQL, e.g.
                'sum by (level) (count_over_time({namespace="prod", app="x"} [15m]))'.
            time: ISO-8601 UTC or unix seconds; default now.
        """
        checked(query)
        if is_log_query(query):
            raise ToolError(
                "query takes a metric query (e.g. count_over_time(...)); "
                "use query_range for log lines"
            )
        try:
            at = parse_time(time, "time") if time else _time.time()
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        data = await guarded(client.query(query, at))
        return {"query": query, "time": iso(at), **shape_result(data, settings)}

    @server.tool()
    async def list_labels(
        start: str | None = None, end: str | None = None, selector: str | None = None
    ) -> dict[str, Any]:
        """Stream label names in [start, end] (default: the last hour).

        Args:
            start: ISO-8601 UTC or unix seconds.
            end: ISO-8601 UTC or unix seconds.
            selector: stream selector to scope to, e.g. '{namespace="prod"}' (default: the
                server's allowlisted streams).
        """
        begin, finish = window(start, end)
        names = sorted(await guarded(client.labels(begin, finish, scope(selector))))
        cap = settings.max_results
        return {"total": len(names), "truncated": len(names) > cap, "labels": names[:cap]}

    @server.tool()
    async def label_values(
        label: str,
        start: str | None = None,
        end: str | None = None,
        selector: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Values of one stream label in [start, end] (default: the last hour).

        Args:
            label: label name, e.g. 'app' or 'level'.
            start: ISO-8601 UTC or unix seconds.
            end: ISO-8601 UTC or unix seconds.
            selector: stream selector to scope to (default: the allowlisted streams).
            limit: max values returned (capped by the server).
        """
        try:
            name = check_label(label)
            cap = validate_limit(limit, settings.max_results)
        except GuardError as exc:
            raise ToolError(str(exc)) from exc
        begin, finish = window(start, end)
        values = sorted(await guarded(client.label_values(name, begin, finish, scope(selector))))
        return {
            "label": name,
            "total": len(values),
            "truncated": len(values) > cap,
            "values": values[:cap],
        }

    return server
