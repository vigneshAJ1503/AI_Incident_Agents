"""Deterministic log analysis: current window vs a 24h baseline -> facts and signals.

The LLM reasons over these results; it doesn't have to count, diff or template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from aiops.agents.log_agent.patterns import Pattern, cluster

#: Signal vocabulary (must match config/prompts/logs/v*.md).
SIGNALS = (
    "db_timeout_errors_up",
    "error_rate_up",
    "new_error_pattern",
    "oom_errors",
    "service_restarts",
    "dependency_timeouts",
    "slow_queries",
    "capacity_degraded",
    "upstream_unavailable",
    "cache_connection_errors",
    "deployment_detected",
    "no_errors",
)

#: Keyword rules applied to NEW or strongly elevated patterns only.
KEYWORD_SIGNALS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"(acquire a connection|connection pool|hikari|jdbc.*timeout|(database|db) connection (timeout|refused|reset))",
            re.I,
        ),
        "db_timeout_errors_up",
    ),
    (re.compile(r"(OutOfMemoryError|out of memory|heap space|GC overhead)", re.I), "oom_errors"),
    (
        re.compile(r"(timeout calling|timed out calling|upstream timeout|deadline exceeded)", re.I),
        "dependency_timeouts",
    ),
    (re.compile(r"slow query", re.I), "slow_queries"),
    (re.compile(r"(ready replicas|queue depth|no capacity|throttl)", re.I), "capacity_degraded"),
    (
        re.compile(r"(no healthy upstream|HTTP 50[23] from|service unavailable)", re.I),
        "upstream_unavailable",
    ),
    (re.compile(r"(redis|memcached|cache unavailable)", re.I), "cache_connection_errors"),
]

#: Signals decided only by the deterministic analysis (an LLM can't add or remove them).
DATA_SIGNALS = frozenset(
    {"error_rate_up", "new_error_pattern", "deployment_detected", "service_restarts", "no_errors"}
)

MIN_NEW_PATTERN_COUNT = 3
ERROR_RATE_FACTOR = 3.0
MIN_ERRORS_FOR_RATE = 10
ELEVATED_FACTOR = 5.0


@dataclass
class PatternDelta:
    pattern: Pattern
    baseline_count: int
    expected_count: float  # baseline scaled to the current window length

    @property
    def is_new(self) -> bool:
        return self.baseline_count == 0

    @property
    def ratio(self) -> float:
        return self.pattern.count / max(self.expected_count, 0.5)


@dataclass
class LogAnalysis:
    current_total: int
    current_errors: int
    baseline_total: int
    baseline_errors: int
    window_ratio: float  # current duration / baseline duration
    deltas: list[PatternDelta] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)  # version, first_seen, count
    restarts: int = 0
    signals: list[str] = field(default_factory=list)

    @property
    def current_error_rate(self) -> float:
        return self.current_errors / self.current_total if self.current_total else 0.0

    @property
    def baseline_error_rate(self) -> float:
        return self.baseline_errors / self.baseline_total if self.baseline_total else 0.0

    @property
    def anomalous(self) -> list[PatternDelta]:
        """New patterns, or recurring ones far above their normal rate."""
        return [
            d
            for d in self.deltas
            if d.pattern.count >= MIN_NEW_PATTERN_COUNT and (d.is_new or d.ratio >= ELEVATED_FACTOR)
        ]

    def lines(self) -> list[str]:
        """Compact text for the LLM prompt."""
        out = [
            f"Current window: {self.current_total} lines, {self.current_errors} errors "
            f"({self.current_error_rate:.2%}). Baseline (previous 24h): {self.baseline_errors} errors "
            f"in {self.baseline_total} lines ({self.baseline_error_rate:.2%}).",
        ]
        if self.anomalous:
            out.append("Anomalous patterns (new or >=5x normal):")
            for d in self.anomalous[:8]:
                status = "NEW" if d.is_new else f"{d.ratio:.0f}x normal"
                out.append(
                    f"  - [{d.pattern.level}] {d.pattern.template} | count={d.pattern.count} | {status} "
                    f"| first_seen={d.pattern.first_seen}"
                )
        others = [d for d in self.deltas if d not in self.anomalous][:5]
        if others:
            out.append("Other patterns (background noise or below the anomaly threshold):")
            out.extend(
                f"  - [{d.pattern.level}] {d.pattern.template} | count={d.pattern.count} "
                f"| baseline_24h={d.baseline_count}"
                for d in others
            )
        for dep in self.deployments:
            out.append(
                f"Deployment detected: version {dep['version']} started at {dep['first_seen']}."
            )
        if self.restarts:
            out.append(f"Service restarts in window: {self.restarts}.")
        out.append(f"Deterministic signals: {', '.join(self.signals) or 'none'}")
        return out


def _rows(data: Any) -> tuple[list[str], list[list[Any]]]:
    if not isinstance(data, dict):
        return [], []
    return list(data.get("columns", [])), list(data.get("rows", []))


def _col(columns: list[str], row: list[Any], name: str) -> Any:
    return row[columns.index(name)] if name in columns else None


def analyze(
    volume: Any,
    patterns: Any,
    lifecycle: Any,
    *,
    error_levels: list[str],
    current: timedelta,
    baseline: timedelta,
) -> LogAnalysis:
    """Inputs are execute_esql results with a ``window`` column (current|baseline)."""
    cols, rows = _rows(volume)
    totals = {"current": [0, 0], "baseline": [0, 0]}
    for row in rows:
        window, level, count = (
            _col(cols, row, "window"),
            _col(cols, row, "level"),
            int(_col(cols, row, "count") or 0),
        )
        if window in totals:
            totals[window][0] += count
            if level in error_levels:
                totals[window][1] += count

    cols, rows = _rows(patterns)
    by_window: dict[str, list[tuple[int, str | None, str | None, str, str]]] = {
        "current": [],
        "baseline": [],
    }
    for row in rows:
        window = _col(cols, row, "window")
        if window in by_window:
            by_window[window].append(
                (
                    int(_col(cols, row, "count") or 0),
                    _col(cols, row, "first_seen"),
                    _col(cols, row, "last_seen"),
                    str(_col(cols, row, "level")),
                    str(_col(cols, row, "msg")),
                )
            )
    window_ratio = current / baseline if baseline else 1.0
    baseline_counts = {(p.level, p.template): p.count for p in cluster(by_window["baseline"])}
    deltas = [
        PatternDelta(
            pattern=p,
            baseline_count=baseline_counts.get((p.level, p.template), 0),
            expected_count=baseline_counts.get((p.level, p.template), 0) * window_ratio,
        )
        for p in cluster(by_window["current"])
    ]

    # lifecycle: STATS count, starts, first_seen BY window, version (over all traffic)
    cols, rows = _rows(lifecycle)
    deployments: list[dict[str, Any]] = []
    restarts = 0
    baseline_versions = {
        _col(cols, r, "version") for r in rows if _col(cols, r, "window") == "baseline"
    }
    for row in rows:
        if _col(cols, row, "window") != "current":
            continue
        version = _col(cols, row, "version")
        if version not in baseline_versions:
            deployments.append({"version": version, "first_seen": _col(cols, row, "first_seen")})
        else:  # startups of an already-running version = restarts
            restarts += int(_col(cols, row, "starts") or 0)

    analysis = LogAnalysis(
        current_total=totals["current"][0],
        current_errors=totals["current"][1],
        baseline_total=totals["baseline"][0],
        baseline_errors=totals["baseline"][1],
        window_ratio=window_ratio,
        deltas=deltas,
        deployments=deployments,
        restarts=restarts,
    )
    analysis.signals = derive_signals(analysis)
    return analysis


def derive_signals(analysis: LogAnalysis) -> list[str]:
    signals: set[str] = set()
    anomalous = analysis.anomalous
    if any(d.is_new and d.pattern.level in ("ERROR", "FATAL", "CRITICAL") for d in anomalous):
        signals.add("new_error_pattern")
    expected_errors = analysis.baseline_errors * analysis.window_ratio
    if (
        analysis.current_errors >= MIN_ERRORS_FOR_RATE
        and analysis.current_errors >= ERROR_RATE_FACTOR * max(expected_errors, 1.0)
    ):
        signals.add("error_rate_up")
    for delta in anomalous:
        for regex, signal in KEYWORD_SIGNALS:
            if regex.search(delta.pattern.template):
                signals.add(signal)
    if analysis.deployments:
        signals.add("deployment_detected")
    if analysis.restarts >= 2:
        signals.add("service_restarts")
    if not signals and analysis.current_errors == 0:
        signals.add("no_errors")
    return [s for s in SIGNALS if s in signals]
