"""Record Log-agent fixtures on REAL Kubernetes logs in LOKI (PR-P4a, AIOPS_PROFILE=local-loki).

The portability proof: the same cluster, the same Fluent Bit, the same Log agent; only the
profile (logs provider = loki) differs from ``record_logs_k8s.py``. The window used is
stored in ``meta.json`` next to the fixtures; replay tests rebuild the exact task from it.

Incident scenarios run inside ``aiops fault run`` from the MAIN clone (one shared lock):

    cd <main clone>/backend && uv run aiops fault run S1 -- \\
        uv --directory <repo>/backend run --no-sync python -m tests.fixtures.record_logs_loki \\
        --out <repo>/backend/tests/fixtures/logs-loki/S1

Healthy S0 (no fault): pass ``--lock-repo <main clone>``; the recorder takes the same lock
itself and refuses to record while a scenario is active.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2

from aiops.agents.deps import build_deps
from aiops.agents.log_agent import LogAgent
from aiops.core.config import load_settings
from aiops.faults import FaultInjector, cluster_lock, kubectl_runner
from aiops.llm.fake import FakeLLMProvider
from tests.fixtures.record_logs_k8s import (
    healthy_versions,
    iso,
    responder,
    task_for_window,
    window_for,
)
from tests.fixtures.scenario_context import SCENARIOS

ENV = "local-loki"
CONFIG = Path(__file__).resolve().parents[3] / "config"
FRESHNESS_S = 20.0
MAX_WAIT_S = 90.0


def _query(loki_url: str, query: str, at: datetime) -> list[dict[str, Any]]:
    response = httpx2.get(
        f"{loki_url}/loki/api/v1/query",
        params={"query": query, "time": str(int(at.timestamp() * 1e9))},
        timeout=30,
    )
    response.raise_for_status()
    result: list[dict[str, Any]] = response.json()["data"]["result"]
    return result


def wait_for_fresh_logs(loki_url: str, service: str) -> str:
    """Block until Fluent Bit has shipped the service's last few seconds of logs to Loki."""
    selector = f'{{namespace="prod", app="{service}"}}'
    deadline = time.monotonic() + MAX_WAIT_S
    last = "never"
    while time.monotonic() < deadline:
        response = httpx2.get(
            f"{loki_url}/loki/api/v1/query_range",
            params={"query": selector, "limit": 1, "direction": "backward", "since": "5m"},
            timeout=10,
        )
        response.raise_for_status()
        streams = response.json()["data"]["result"]
        stamps = [int(v[0]) for s in streams for v in s["values"]]
        if stamps:
            newest = datetime.fromtimestamp(max(stamps) / 1e9, UTC)
            last = iso(newest)
            if (datetime.now(UTC) - newest).total_seconds() <= FRESHNESS_S:
                return last
        time.sleep(2)
    raise SystemExit(f"logs of {service} are stale in Loki (last: {last}): is Fluent Bit up?")


def pollution(
    loki_url: str, service: str, start: datetime, end: datetime, *, errors: bool = True
) -> list[str]:
    """Non-base versions (and, with ``errors``, ERROR lines) of ``service`` in [start, end)."""
    healthy = healthy_versions(service)
    seconds = max(int((end - start).total_seconds()), 1)
    selector = f'{{namespace="prod", app="{service}"}}'
    lines = _query(
        loki_url,
        f'sum by (version) (count_over_time({selector} | json version="version" '
        f"| drop __error__, __error_details__ [{seconds}s]))",
        end,
    )
    errs = _query(
        loki_url,
        f'sum by (version) (count_over_time({{namespace="prod", app="{service}", level="ERROR"}} '
        f'| json version="version" | drop __error__, __error_details__ [{seconds}s]))',
        end,
    )
    n_errors = {s["metric"].get("version"): int(float(s["value"][1])) for s in errs}
    dirty = []
    for series in lines:
        version = series["metric"].get("version")
        n = int(float(series["value"][1]))
        bad = n_errors.get(version, 0)
        if (errors and bad) or (version and version not in healthy):
            dirty.append(f"{version}: {bad} errors in {n} lines")
    return dirty


async def record(scenario: str, out: Path, start: datetime, end: datetime) -> None:
    settings = load_settings(ENV, CONFIG)
    llm = FakeLLMProvider(responder=responder)
    deps = build_deps(settings, llm=llm, record_dir=out)
    task = task_for_window(scenario, start, end)
    result = await LogAgent(deps).run(task)
    meta = {
        "scenario": scenario,
        "environment": ENV,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "incident_start": os.environ.get("AIOPS_INCIDENT_START"),
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(scenario, result.status.value, result.signals, [e.summary for e in result.evidence])


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--scenario", default=os.environ.get("AIOPS_SCENARIO", "S0"))
    parser.add_argument("--out", type=Path, required=True, help="Fixture directory (absolute).")
    parser.add_argument(
        "--lock-repo", type=Path, help="Main clone: hold its cluster lock (healthy S0 recording)."
    )
    parser.add_argument("--lead-minutes", type=float, default=10.0)
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument(
        "--loki-url", default="http://localhost:3101", help="Loki as seen from the host."
    )
    parser.add_argument(
        "--rerun",
        action="store_true",
        help="Re-query the window of the existing meta.json in --out (Loki keeps 2 days): "
        "refreshes the fixtures after an adapter fix without re-injecting the fault.",
    )
    args = parser.parse_args()
    scenario = args.scenario.upper()
    out: Path = args.out.resolve()
    if args.rerun:
        old = json.loads((out / "meta.json").read_text())
        if old.get("incident_start"):
            os.environ["AIOPS_INCIDENT_START"] = old["incident_start"]
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(
                record(
                    old["scenario"],
                    Path(tmp),
                    datetime.fromisoformat(old["start"]),
                    datetime.fromisoformat(old["end"]),
                )
            )
            shutil.rmtree(out, ignore_errors=True)
            shutil.copytree(tmp, out)
        return
    service = SCENARIOS[scenario].service or "payment-service"
    baseline = timedelta(
        hours=float(load_settings(ENV, CONFIG).capability("logs").settings["baseline_hours"])
    )

    def run() -> None:
        last = wait_for_fresh_logs(args.loki_url, service)
        end = datetime.now(UTC).replace(microsecond=0)
        start, end = window_for(scenario, end, timedelta(minutes=args.lead_minutes))
        if SCENARIOS[scenario].healthy:
            dirty = pollution(args.loki_url, service, start, end)
            # A (re)started cluster restarts pods: startups in the window are not "healthy".
            seconds = int((end - start).total_seconds())
            starts = _query(
                args.loki_url,
                f'sum(count_over_time({{namespace="prod", app="{service}"}} '
                f'|= "Starting" [{seconds}s]))',
                end,
            )
            if starts and int(float(starts[0]["value"][1])):
                dirty.append(f"{starts[0]['value'][1]} startup lines in the window")
            where = f"window {iso(start)}..{iso(end)}"
        else:
            injected = start + timedelta(minutes=args.lead_minutes)
            dirty = pollution(args.loki_url, service, start - baseline, start, errors=False)
            dirty += pollution(args.loki_url, service, start, injected)
            where = f"before the injection {iso(start - baseline)}..{iso(injected)}"
        print(f"latest {service} log in Loki: {last}; {where}: {dirty or 'clean'}")
        if dirty:
            raise SystemExit(3)
        if args.check_only:
            return
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(record(scenario, Path(tmp), start, end))
            shutil.rmtree(out, ignore_errors=True)
            shutil.copytree(tmp, out)

    if args.lock_repo is None:
        run()
        return
    injector = FaultInjector(args.lock_repo.resolve(), kubectl_runner("aiops"))
    with cluster_lock(injector.lock_path, wait=True):
        active = injector.state().scenario
        if active:
            raise SystemExit(f"scenario {active} is active: revert it before recording {scenario}")
        run()


if __name__ == "__main__":
    main()
