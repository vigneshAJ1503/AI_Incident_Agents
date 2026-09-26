"""Alert history from Prometheus's ``ALERTS`` series (read-only, optional).

Prometheus writes one ``ALERTS{alertname, alertstate, <alert labels>}`` sample per rule
evaluation while an alert is pending or firing. A range query over it gives the firing
intervals of every alert, including the ones that already resolved, which Alertmanager
cannot provide.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import httpx2

from alertmanager_mcp.config import ServerSettings

#: At most this many points per series (the step grows with the range).
MAX_POINTS = 1_000
#: ALERTS labels that are not alert labels.
DROPPED_LABELS = frozenset({"__name__", "alertstate"})


class PrometheusError(Exception):
    pass


class PrometheusClient:
    def __init__(self, settings: ServerSettings, http: httpx2.AsyncClient | None = None) -> None:
        if not settings.prometheus_url and http is None:
            raise ValueError("PROMETHEUS_URL is not set")
        self._http = http or httpx2.AsyncClient(
            base_url=settings.prometheus_url or "",
            headers={"Accept": "application/json"},
            timeout=settings.query_timeout_s,
        )
        self._min_step_s = settings.history_step_s

    async def aclose(self) -> None:
        await self._http.aclose()

    def step_for(self, begin: datetime, finish: datetime) -> int:
        span = (finish - begin).total_seconds()
        return max(self._min_step_s, math.ceil(span / MAX_POINTS))

    async def firing_series(
        self, matchers: list[str], begin: datetime, finish: datetime
    ) -> tuple[list[dict[str, Any]], int]:
        """``ALERTS{alertstate="firing", <matchers>}`` over the range; returns (series, step)."""
        step = self.step_for(begin, finish)
        selector = ",".join(['alertstate="firing"', *matchers])
        params = {
            "query": f"ALERTS{{{selector}}}",
            "start": f"{begin.timestamp():.0f}",
            "end": f"{finish.timestamp():.0f}",
            "step": str(step),
        }
        try:
            response = await self._http.get("/api/v1/query_range", params=params)
        except httpx2.HTTPError as exc:
            raise PrometheusError(f"Prometheus unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise PrometheusError(f"Prometheus error {response.status_code}: {response.text[:300]}")
        body = response.json()
        if body.get("status") != "success":
            raise PrometheusError(f"Prometheus error: {body.get('error', 'unknown')}")
        result: list[dict[str, Any]] = body.get("data", {}).get("result", [])
        return result, step


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def firing_intervals(
    series: list[dict[str, Any]], step: int, begin: datetime, finish: datetime
) -> list[dict[str, Any]]:
    """One entry per (alert, continuous firing interval), oldest first.

    Samples further apart than 1.5 steps start a new interval. ``resolved_at`` is None when
    the alert was still firing at the end of the range; ``started_before_range`` is True
    when it was already firing at the start (its real start is earlier).
    """
    out: list[dict[str, Any]] = []
    begin_ts, end_ts = begin.timestamp(), finish.timestamp()
    for s in series:
        labels = {k: v for k, v in s.get("metric", {}).items() if k not in DROPPED_LABELS}
        stamps = sorted(float(t) for t, _ in s.get("values", []))
        intervals: list[tuple[float, float]] = []
        for ts in stamps:
            if intervals and ts - intervals[-1][1] <= step * 1.5:
                intervals[-1] = (intervals[-1][0], ts)
            else:
                intervals.append((ts, ts))
        for first, last in intervals:
            still = end_ts - last <= step * 1.5
            out.append(
                {
                    "alertname": labels.get("alertname"),
                    "service": labels.get("service"),
                    "severity": labels.get("severity"),
                    "state": "firing" if still else "resolved",
                    "firing_since": _iso(first),
                    "resolved_at": None if still else _iso(last + step),
                    "started_before_range": first - begin_ts <= step * 1.5,
                    "labels": labels,
                    "source": "prometheus",
                }
            )
    out.sort(key=lambda a: (a["firing_since"], str(a["alertname"])))
    return out
