"""Record Metrics agent MCP fixtures from a LIVE fault (real Prometheus data).

Metrics can't be seeded at a fixed time like logs or alerts: they are recorded while a
real fault runs, and the window used is stored in ``meta.json`` next to the fixture
(the replay task is rebuilt from it). Run from the MAIN clone (it holds the cluster lock):

    cd backend
    uv run aiops fault run S1 -- uv run python -m tests.fixtures.record_metrics --out <dir>
    uv run python -m tests.fixtures.record_metrics --scenario S0 --data-dir ../.data  # no fault

(`make record-metrics S=S1` does the first one.) Inside ``fault run`` the command gets
AIOPS_SCENARIO and AIOPS_INCIDENT_START, and the fault stays active while this polls:
it waits until ``--after`` minutes past the injection so Prometheus has a few minutes
of fault data (rate windows, `for` durations), then records the window ending now.
For S0 it takes the cluster lock itself and refuses to record while a fault is active.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops.agents.deps import build_deps
from aiops.agents.metrics_agent import MetricsAgent
from aiops.core.config import load_settings
from aiops.core.models import TimeRange
from aiops.faults import FaultState, cluster_lock
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.fixtures.scenario_context import SCENARIOS

HERE = Path(__file__).parent
REPO = Path(__file__).resolve().parents[3]


def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """Minimal submit: recording captures the deterministic tool exchanges only."""
    return tool_call("submit", {"status": "no_signal", "summary": "recording", "confidence": 0.1})


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def minute(ts: datetime) -> datetime:
    """Floor to the minute: stable, readable windows."""
    return ts.replace(second=0, microsecond=0)


async def record(scenario: str, out: Path, window: timedelta, incident: datetime | None) -> None:
    end = minute(datetime.now(UTC))
    time_range = TimeRange(start=end - window, end=end)
    base = SCENARIOS[scenario].task("metrics", end)
    task = base.model_copy(
        update={"context": base.context.model_copy(update={"time_range": time_range})}
    )
    settings = load_settings("local")
    target = out / scenario
    target.mkdir(parents=True, exist_ok=True)
    for stale in target.glob("*.json"):
        stale.unlink()
    deps = build_deps(settings, llm=FakeLLMProvider(responder=responder), record_dir=target)
    result = await MetricsAgent(deps).run(task)
    meta = {
        "scenario": scenario,
        "start": iso(time_range.start),
        "end": iso(time_range.end),
        "incident_start": iso(incident) if incident else None,
        "recorded_at": iso(datetime.now(UTC)),
        "source": "live Prometheus (aiops fault run)" if incident else "live Prometheus, no fault",
    }
    (target / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(scenario, result.status.value, result.signals, len(result.evidence), "evidence")
    for ev in result.evidence:
        print("  ", ev.summary[:220])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--scenario", default=os.environ.get("AIOPS_SCENARIO"))
    parser.add_argument("--out", type=Path, default=HERE / "metrics")
    parser.add_argument("--window-minutes", type=float, default=15)
    parser.add_argument(
        "--after", type=float, default=6.0, help="minutes after injection to record at"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=REPO / ".data",
        help="where cluster.lock / fault-state.json live (the MAIN clone's .data for S0)",
    )
    args = parser.parse_args()
    if not args.scenario:
        parser.error("--scenario (or AIOPS_SCENARIO from `aiops fault run`) is required")
    window = timedelta(minutes=args.window_minutes)
    started = os.environ.get("AIOPS_INCIDENT_START")
    if args.scenario == "S0":
        with cluster_lock(args.data_dir / "cluster.lock", wait=True):
            active = FaultState.load(args.data_dir / "fault-state.json").scenario
            if active:
                raise SystemExit(f"Scenario {active} is active: S0 must be recorded healthy.")
            asyncio.run(record("S0", args.out, window, None))
        return
    if not started:
        raise SystemExit("Run inside `aiops fault run <S> -- ...` (AIOPS_INCIDENT_START unset).")
    incident = datetime.fromisoformat(started.replace("Z", "+00:00"))
    ready = incident + timedelta(minutes=args.after)
    while datetime.now(UTC) < ready:  # the fault stays active while we wait
        time.sleep(5)
    asyncio.run(record(args.scenario, args.out, window, incident))


if __name__ == "__main__":
    main()
