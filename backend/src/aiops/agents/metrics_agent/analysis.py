"""Deterministic metric anomaly detection: baseline vs current, change point, magnitude.

The LLM never does arithmetic on series. For every (metric, service) this module computes
the baseline (median of the window before the incident window), whether and when the
series left it (change point: the first point that starts a sustained run of anomalous
points), the magnitude (median during the anomaly, peak, ratio, z-score), and the
signals. The agent reports them; the LLM reasons over them.
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from aiops.core.models import TimeRange

#: Signal vocabulary (must match config/prompts/metrics/v*.md). All are data-derived.
SIGNALS = (
    "error_rate_up",
    "latency_up",
    "traffic_drop",
    "traffic_spike",
    "db_pool_saturated",
    "memory_pressure",
    "cache_down",
    "dependency_latency_up",
    "no_anomaly",
)
NO_ANOMALY_PHRASE = "No metric anomaly"
SYMPTOM_NOTE = (
    "Metrics show symptoms and their timing (what changed, when, how much), not causes: "
    "correlate them, and keep causal claims as hypotheses."
)

Values = list[tuple[float, float | None]]
Direction = Literal["up", "down"]

#: A run must have at least this many anomalous points among the next SUSTAIN_OF points
#: (at the default 30 s step: 3 of 4 points = sustained for about 1.5 minutes).
SUSTAIN = 3
SUSTAIN_OF = 4
#: Points kept per series for charts.
CHART_POINTS = 40


@dataclass(frozen=True)
class Rule:
    """When a point counts as anomalous against a baseline ``b``.

    up:   v >= b + min_delta, and v >= b * min_ratio (if set), and v >= level (if set)
    down: v <= b - min_delta, and v <= b * min_ratio (if set), and v < level (if set),
          and only judged when b >= min_baseline
    """

    direction: Direction
    min_delta: float
    min_ratio: float | None = None
    level: float | None = None
    min_baseline: float = 0.0

    def anomalous(self, value: float, baseline: float | None) -> bool:
        b = baseline if baseline is not None else 0.0
        if self.direction == "up":
            if value < b + self.min_delta:
                return False
            if self.min_ratio is not None and b > 0 and value < b * self.min_ratio:
                return False
            return self.level is None or value >= self.level
        if baseline is None or b < self.min_baseline or value > b - self.min_delta:
            return False
        if self.min_ratio is not None and value > b * self.min_ratio:
            return False
        return self.level is None or value < self.level


#: Per-metric rules. Thresholds are deliberately conservative: S0 must stay silent.
RULES: dict[str, tuple[Rule, ...]] = {
    "error_rate": (Rule("up", min_delta=0.01, min_ratio=3.0),),
    "latency_p95": (Rule("up", min_delta=0.2, min_ratio=2.0),),
    "latency_p99": (Rule("up", min_delta=0.25, min_ratio=2.0),),
    "rps": (
        Rule("down", min_delta=0.2, min_ratio=0.5, min_baseline=0.2),
        Rule("up", min_delta=1.0, min_ratio=2.0),
    ),
    "db_pool_utilization": (Rule("up", min_delta=0.2, level=0.9),),
    "db_pool_pending": (Rule("up", min_delta=1.0, level=1.0),),
    "cache_up": (Rule("down", min_delta=0.5, level=0.5, min_baseline=0.5),),
    "memory_rss": (Rule("up", min_delta=20e6, min_ratio=1.5),),
}


def iso(ts: float | datetime) -> str:
    moment = ts if isinstance(ts, datetime) else datetime.fromtimestamp(ts, UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def fmt(value: float | None, unit: str) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    if unit == "ratio":
        return f"{value * 100:.1f}%"
    if unit == "seconds":
        return f"{value * 1000:.0f}ms" if value < 1 else f"{value:.2f}s"
    if unit == "bytes":
        return f"{value / 1e6:.0f}MB"
    if unit == "rps":
        return f"{value:.2f} req/s"
    if unit == "bool":
        return "up" if value >= 0.5 else "DOWN"
    return f"{value:g}"


def parse_series(data: Any, group_label: str) -> dict[str, Values]:
    """prometheus-mcp query_range result -> {label value: [(ts, value)]}."""
    out: dict[str, Values] = {}
    series = data.get("series", []) if isinstance(data, dict) else []
    for s in series:
        if not isinstance(s, dict):
            continue
        key = (s.get("labels") or {}).get(group_label)
        if not key:
            continue
        values: Values = []
        for point in s.get("values") or []:
            if not isinstance(point, list | tuple) or len(point) != 2:
                continue
            ts, raw = point
            value = float(raw) if isinstance(raw, int | float) and math.isfinite(raw) else None
            values.append((float(ts), value))
        out[str(key)] = values
    return out


def downsample(values: Values, points: int = CHART_POINTS) -> list[list[float | None]]:
    if len(values) <= points:
        return [[int(t), v] for t, v in values]
    stride = math.ceil(len(values) / points)
    kept = values[::stride]
    if kept[-1] != values[-1]:
        kept.append(values[-1])
    return [[int(t), v] for t, v in kept]


@dataclass
class Anomaly:
    metric: str
    service: str
    direction: Direction
    baseline: float | None
    current: float  # median while anomalous
    peak: float
    start_time: datetime
    ongoing: bool
    unit: str
    zscore: float | None = None

    @property
    def ratio(self) -> float | None:
        if self.baseline is None or self.baseline == 0:
            return None
        return self.current / self.baseline

    def describe(self) -> str:
        change = "up" if self.direction == "up" else "down"
        ratio = f", x{self.ratio:.1f}" if self.ratio is not None else ""
        state = "ongoing" if self.ongoing else "recovered"
        return (
            f"{self.metric.replace('_', ' ')} {change}: {fmt(self.baseline, self.unit)} -> "
            f"{fmt(self.current, self.unit)} (peak {fmt(self.peak, self.unit)}{ratio}) "
            f"since {iso(self.start_time)}, {state}"
        )


@dataclass
class SeriesStats:
    metric: str
    service: str
    unit: str
    baseline: float | None
    current: float | None  # median over the window (or during the anomaly)
    peak: float | None
    anomaly: Anomaly | None = None
    points: int = 0
    note: str | None = None  # e.g. "3 restarts in the window"

    def as_dict(self) -> dict[str, Any]:
        a = self.anomaly
        return {
            "baseline": self.baseline,
            "current": self.current,
            "peak": self.peak,
            "anomalous": a is not None,
            "direction": a.direction if a else None,
            "start_time": iso(a.start_time) if a else None,
            "ongoing": a.ongoing if a else None,
            "ratio": round(a.ratio, 2) if a and a.ratio is not None else None,
            "zscore": round(a.zscore, 1) if a and a.zscore is not None else None,
            "note": self.note,
        }


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def detect(
    metric: str,
    service: str,
    unit: str,
    values: Values,
    window: TimeRange,
) -> SeriesStats:
    """Baseline = points before ``window.start``; change point searched inside the window."""
    start_ts = window.start.timestamp()
    base = [v for t, v in values if t < start_ts and v is not None]
    inside = [(t, v) for t, v in values if t >= start_ts and v is not None]
    baseline = _median(base)
    stats = SeriesStats(
        metric=metric,
        service=service,
        unit=unit,
        baseline=baseline,
        current=_median([v for _, v in inside]),
        peak=max((v for _, v in inside), default=None),
        points=len(inside),
    )
    for rule in RULES.get(metric, ()):
        flags = [rule.anomalous(v, baseline) for _, v in inside]
        start = next(
            (
                i
                for i, flag in enumerate(flags)
                if flag
                and sum(flags[i : i + SUSTAIN_OF]) >= min(SUSTAIN, len(flags) - i)
                and sum(flags[i:]) >= SUSTAIN
            ),
            None,
        )
        if start is None:
            continue
        during = [v for (_, v), flag in zip(inside[start:], flags[start:], strict=True) if flag]
        current = statistics.median(during)
        peak = max(during) if rule.direction == "up" else min(during)
        spread = statistics.pstdev(base) if len(base) >= 2 else 0.0
        stats.anomaly = Anomaly(
            metric=metric,
            service=service,
            direction=rule.direction,
            baseline=baseline,
            current=current,
            peak=peak,
            start_time=datetime.fromtimestamp(inside[start][0], UTC),
            ongoing=all(flags[-2:]),
            unit=unit,
            zscore=(current - (baseline or 0.0)) / spread if spread > 0 else None,
        )
        stats.current, stats.peak = current, peak
        break
    return stats


def restarts_stats(service: str, values: Values, window: TimeRange) -> SeriesStats:
    """Restart counter (max over the app's pods): increments inside the window."""
    start_ts = window.start.timestamp()
    known = [(t, v) for t, v in values if v is not None]
    increments = 0.0
    first: float | None = None
    for (_, prev), (t, cur) in itertools.pairwise(known):
        if t >= start_ts and cur is not None and prev is not None and cur > prev:
            increments += cur - prev
            first = first if first is not None else t
    stats = SeriesStats(
        metric="restarts",
        service=service,
        unit="count",
        baseline=None,
        current=increments,
        peak=max((v for _, v in known), default=None),
        points=len(known),
        note=f"{increments:g} container restart(s) in the window" if increments else None,
    )
    if increments and first is not None:
        stats.anomaly = Anomaly(
            metric="restarts",
            service=service,
            direction="up",
            baseline=0.0,
            current=increments,
            peak=increments,
            start_time=datetime.fromtimestamp(first, UTC),
            ongoing=False,
            unit="count",
        )
    return stats


def oom_stats(service: str, values: Values, window: TimeRange) -> SeriesStats:
    """OOMKilled as the last termination reason, first seen inside the window."""
    start_ts = window.start.timestamp()
    before = any(v == 1 for t, v in values if t < start_ts)
    hits = [t for t, v in values if t >= start_ts and v == 1]
    stats = SeriesStats(
        metric="oom_killed",
        service=service,
        unit="bool",
        baseline=1.0 if before else 0.0,
        current=1.0 if hits else 0.0,
        peak=1.0 if hits else 0.0,
        points=len(values),
    )
    if hits and not before:
        stats.note = "container OOMKilled in the window"
        stats.anomaly = Anomaly(
            metric="oom_killed",
            service=service,
            direction="up",
            baseline=0.0,
            current=1.0,
            peak=1.0,
            start_time=datetime.fromtimestamp(hits[0], UTC),
            ongoing=False,
            unit="bool",
        )
    return stats


@dataclass
class MetricsAnalysis:
    service: str
    dependencies: list[str]
    window: TimeRange
    baseline_window: TimeRange
    stats: list[SeriesStats] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)  # metric keys whose query failed

    def of(self, service: str, metric: str) -> SeriesStats | None:
        return next((s for s in self.stats if s.service == service and s.metric == metric), None)

    def anomalies(self, service: str | None = None) -> list[Anomaly]:
        found = [s.anomaly for s in self.stats if s.anomaly is not None]
        chosen = [a for a in found if service is None or a.service == service]
        return sorted(chosen, key=lambda a: a.start_time)

    def _has(self, service: str, metric: str, direction: Direction | None = None) -> bool:
        s = self.of(service, metric)
        return bool(s and s.anomaly and (direction is None or s.anomaly.direction == direction))

    @property
    def signals(self) -> list[str]:
        me = self.service
        found: set[str] = set()
        if self._has(me, "error_rate"):
            found.add("error_rate_up")
        if self._has(me, "latency_p95") or self._has(me, "latency_p99"):
            found.add("latency_up")
        if self._has(me, "rps", "down"):
            found.add("traffic_drop")
        if self._has(me, "rps", "up"):
            found.add("traffic_spike")
        if self._has(me, "db_pool_utilization") or self._has(me, "db_pool_pending"):
            found.add("db_pool_saturated")
        if self._has(me, "oom_killed") or (
            self._has(me, "memory_rss") and self._has(me, "restarts")
        ):
            found.add("memory_pressure")
        if any(self._has(s, "cache_up") for s in [me, *self.dependencies]):
            found.add("cache_down")
        if any(
            self._has(d, "latency_p95") or self._has(d, "latency_p99") for d in self.dependencies
        ):
            found.add("dependency_latency_up")
        if not found and not self.missing:
            found.add("no_anomaly")
        return [s for s in SIGNALS if s in found]

    @property
    def first_anomaly(self) -> Anomaly | None:
        own = self.anomalies(self.service)
        return own[0] if own else None

    def no_anomaly_sentence(self) -> str:
        deps = f" or its dependencies ({', '.join(self.dependencies)})" if self.dependencies else ""
        return (
            f"{NO_ANOMALY_PHRASE} for {self.service}{deps}: error rate, latency, traffic, "
            "saturation and memory stayed within their baseline."
        )

    def lines(self) -> list[str]:
        """Compact text for the LLM prompt."""
        w, b = self.window, self.baseline_window
        out = [
            f"Incident window: {iso(w.start)} to {iso(w.end)}; baseline: {iso(b.start)} to "
            f"{iso(b.end)} (medians compared; anomalies need about 1.5 minutes of sustained "
            "deviation).",
        ]
        own = self.anomalies(self.service)
        if own:
            out.append(f"Anomalies on {self.service} ({len(own)}), earliest first:")
            out.extend(f"  - {a.describe()}" for a in own)
        else:
            out.append(f"No anomaly on {self.service} itself.")
        normal = [
            f"{s.metric} {fmt(s.current, s.unit)}"
            for s in self.stats
            if s.service == self.service
            and s.anomaly is None
            and s.current is not None
            and s.metric not in ("restarts", "oom_killed")
        ]
        if normal:
            out.append(f"Within baseline on {self.service}: " + ", ".join(normal) + ".")
        for dep in self.dependencies:
            dep_anomalies = self.anomalies(dep)
            if dep_anomalies:
                out.append(f"Dependency {dep} ({len(dep_anomalies)}):")
                out.extend(f"  - {a.describe()}" for a in dep_anomalies)
            else:
                out.append(f"Dependency {dep}: no anomaly.")
        notes = [f"{s.service}: {s.note}" for s in self.stats if s.note]
        if notes:
            out.append("Kubernetes: " + "; ".join(notes) + ".")
        first = self.first_anomaly
        if first is not None:
            out.append(
                f"First anomaly on {self.service}: {first.metric} at {iso(first.start_time)}."
            )
        if not self.anomalies() and not self.missing:
            out.append(self.no_anomaly_sentence())
        if self.missing:
            out.append(f"Could not query: {', '.join(self.missing)}.")
        out.append(SYMPTOM_NOTE)
        out.append(f"Deterministic signals: {', '.join(self.signals) or 'none'}")
        return out
