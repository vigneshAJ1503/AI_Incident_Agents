"""Record K8s agent MCP fixtures LIVE from Minikube, one scenario at a time.

Incidents S1-S5 run inside the fault framework, which holds the cluster lock across
inject -> settle -> this recorder -> revert (run it from the MAIN clone so the lock
is shared; point --out at the checkout that should receive the fixtures):

    cd backend && uv run aiops fault run S2 -- \\
        uv run --directory "$PWD" python -m tests.fixtures.record_k8s --out "$PWD/tests/fixtures/k8s"

S0 (healthy) needs no fault: it takes the lock itself, checks nothing is injected and
records the last few minutes:

    uv run python -m tests.fixtures.record_k8s --scenario S0 --repo-root /path/to/main/clone

Live fixtures can't be anchored at FIXED_NOW, so the window used is stored next to
them in ``meta.json`` (``start``, ``end``, ``incident_start``) and replays reuse it
(``aiops.evals.replay.replay_task``). The window starts shortly before the injection,
but never before the fault framework's previous baseline reset: a revert restarts
every service ("revert to baseline" rollouts), often seconds before the next
injection, and that harness churn must not look like part of the incident.
Needs: make k8s-reader-kubeconfig kubernetes-mcp-up.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aiops.agents.deps import build_deps
from aiops.agents.k8s_agent import K8sAgent
from aiops.core.config import load_settings
from aiops.evals.replay import ReplayMeta
from aiops.faults import FaultState, cluster_lock
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.fixtures.scenario_context import SCENARIOS

HERE = Path(__file__).parent
#: How far before the injection the window starts (S1-S5).
LEAD = timedelta(seconds=60)
#: Length of the healthy S0 window.
S0_WINDOW = timedelta(minutes=10)
NAMESPACE = "prod"
CHANGE_CAUSE = "kubernetes.io/change-cause"
RESET_CAUSE = "revert to baseline"  # aiops.faults.FaultInjector._revert


def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """Minimal submit: recording captures the deterministic tool exchanges only."""
    return tool_call("submit", {"status": "no_signal", "summary": "recording", "confidence": 0.1})


def now_s() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def last_reset(before: datetime) -> datetime | None:
    """When the fault framework last reset the baseline: its "revert to baseline"
    ReplicaSets and the completion of any rollout (Progressing=NewReplicaSetAvailable)."""
    out = subprocess.run(  # noqa: S603 - fixed argv
        ["kubectl", "--context", "aiops", "-n", NAMESPACE, "get", "deploy,rs", "-o", "json"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    times: list[datetime] = []
    for item in json.loads(out)["items"]:
        meta = item["metadata"]
        if item["kind"] == "ReplicaSet":
            if (meta.get("annotations") or {}).get(CHANGE_CAUSE) == RESET_CAUSE:
                times.append(parse(meta["creationTimestamp"]))
            continue
        for cond in item.get("status", {}).get("conditions") or []:
            if cond.get("type") == "Progressing" and cond.get("reason") == "NewReplicaSetAvailable":
                times.append(parse(cond["lastUpdateTime"]))
    times = [t for t in times if t <= before]
    return max(times) if times else None


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def wait_until_quiet(root: Path, poll_s: float = 30.0) -> None:
    """Wait for a full S0 window without faults or rollouts (other sessions share the cluster)."""
    while True:
        now = now_s()
        reset = last_reset(now)
        active = FaultState.load(root / ".data" / "fault-state.json").scenario
        if not active and (reset is None or reset <= now - S0_WINDOW):
            return
        print(f"  waiting for a quiet cluster (fault={active}, last change={reset})", flush=True)
        time.sleep(poll_s)


def window_start(nominal: datetime, before: datetime) -> datetime:
    reset = last_reset(before)
    if reset is not None and reset >= nominal:
        print(f"  window clipped to start after the baseline reset at {reset.isoformat()}")
        return reset + timedelta(seconds=1)
    return nominal


async def record(
    scenario: str, start: datetime, end: datetime, incident: datetime | None, out: Path, note: str
) -> None:
    fixture_dir = out / scenario
    meta = ReplayMeta(
        scenario=scenario,
        start=start,
        end=end,
        incident_start=incident,
        recorded_at=now_s(),
        note=note,
    )
    task = meta.apply(SCENARIOS[scenario].task("k8s", end))
    settings = load_settings("local")
    deps = build_deps(settings, llm=FakeLLMProvider(responder=responder), record_dir=fixture_dir)
    result = await K8sAgent(deps).run(task)
    meta.save(fixture_dir)
    print(f"{scenario}: {result.status.value} {result.signals}")
    print(f"  window {start.isoformat()} .. {end.isoformat()} (incident {incident})")
    print(f"  {result.summary[:400]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", default=os.environ.get("AIOPS_SCENARIO", ""))
    parser.add_argument("--out", type=Path, default=HERE / "k8s")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="S0: the clone whose .data/cluster.lock and fault-state.json guard the cluster.",
    )
    parser.add_argument(
        "--wait-quiet",
        action="store_true",
        help="S0: first wait until no fault is active and nothing rolled out for a full window.",
    )
    args = parser.parse_args()
    scenario = args.scenario.upper()
    if scenario not in SCENARIOS:
        raise SystemExit(f"unknown scenario '{args.scenario}' (set AIOPS_SCENARIO or --scenario)")

    if scenario == "S0":
        root = args.repo_root or HERE.parents[2]
        if args.wait_quiet:
            wait_until_quiet(root)
        with cluster_lock(root / ".data" / "cluster.lock", wait=True):
            active = FaultState.load(root / ".data" / "fault-state.json").scenario
            if active:
                raise SystemExit(f"{active} is active: S0 must be recorded on a healthy cluster")
            end = now_s()
            start = window_start(end - S0_WINDOW, end)
            asyncio.run(record("S0", start, end, None, args.out, "healthy: no fault injected"))
        return

    raw = os.environ.get("AIOPS_INCIDENT_START")
    if not raw:
        raise SystemExit("AIOPS_INCIDENT_START is not set: run me under `aiops fault run`")
    incident = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(UTC)
    asyncio.run(
        record(
            scenario,
            window_start(incident - LEAD, incident),
            now_s(),
            incident,
            args.out,
            f"live: aiops fault run {scenario} (injected {raw})",
        )
    )


if __name__ == "__main__":
    main()
