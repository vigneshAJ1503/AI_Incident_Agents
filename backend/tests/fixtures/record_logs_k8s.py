"""Record Log-agent fixtures on REAL Kubernetes logs (PR-016, AIOPS_ENV=local-k8s).

Live incidents aren't at a fixed time, so the window used is stored in ``meta.json``
next to the fixtures; replay tests rebuild the exact task from it.

Incident scenarios run inside ``aiops fault run`` (it holds the cluster lock across
inject -> settle -> this command -> revert). Run it from the MAIN clone, so every agent
shares one lock; the output path can point anywhere (e.g. a worktree):

    cd <main clone>/backend && uv run aiops fault run S1 -- \\
        uv --directory <repo>/backend run --no-sync python -m tests.fixtures.record_logs_k8s \\
        --out <repo>/backend/tests/fixtures/logs-k8s/S1

Healthy S0 (no fault): pass ``--lock-repo <main clone>``; the recorder takes the same
lock itself and refuses to record while a scenario is active.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
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
from aiops.core.models import AgentTask, TimeRange
from aiops.faults import FaultInjector, cluster_lock, kubectl_runner
from aiops.llm.base import ChatMessage, LLMResponse, ToolSpec
from aiops.llm.fake import FakeLLMProvider, tool_call
from tests.fixtures.scenario_context import SCENARIOS

ENV = "local-k8s"
CONFIG = Path(__file__).resolve().parents[3] / "config"
BASE_MANIFESTS = CONFIG.parent / "deploy" / "k8s" / "base"
FRESHNESS_S = 20.0
MAX_WAIT_S = 90.0


def responder(messages: list[ChatMessage], tools: list[ToolSpec] | None) -> LLMResponse:
    """Minimal submit: recording captures the deterministic tool exchanges only."""
    return tool_call("submit", {"status": "no_signal", "summary": "recording", "confidence": 0.1})


def wait_for_fresh_logs(es_url: str, service: str) -> str:
    """Block until Fluent Bit has shipped the service's last few seconds of logs."""
    query = {
        "query": f'FROM logs-k8s-* | WHERE kubernetes.labels.app == "{service}" '
        "| STATS newest = MAX(@timestamp)"
    }
    deadline = time.monotonic() + MAX_WAIT_S
    last = "never"
    while time.monotonic() < deadline:
        response = httpx2.post(f"{es_url}/_query", json=query, timeout=10)
        response.raise_for_status()
        values = response.json().get("values") or [[None]]
        last = values[0][0] or "never"
        if last != "never":
            age = (
                datetime.now(UTC) - datetime.fromisoformat(last.replace("Z", "+00:00"))
            ).total_seconds()
            if age <= FRESHNESS_S:
                return str(last)
        time.sleep(2)
    raise SystemExit(
        f"logs of {service} are stale in Elasticsearch (last: {last}): is Fluent Bit up?"
    )


def esql_rows(es_url: str, query: str) -> list[list[Any]]:
    response = httpx2.post(f"{es_url}/_query", json={"query": query}, timeout=30)
    response.raise_for_status()
    rows: list[list[Any]] = response.json().get("values") or []
    return rows


def pollution(
    es_url: str, service: str, start: datetime, end: datetime, *, errors: bool = True
) -> list[str]:
    """Non-base versions (and, with ``errors``, ERROR lines) of ``service`` in [start, end)."""
    healthy = healthy_versions(service)
    rows = esql_rows(
        es_url,
        f'FROM logs-k8s-* | WHERE kubernetes.labels.app == "{service}" '
        f'AND @timestamp >= TO_DATETIME("{iso(start)}") AND @timestamp < TO_DATETIME("{iso(end)}") '
        '| EVAL err = CASE(level == "ERROR", 1, 0) '
        "| STATS lines = COUNT(*), errors = SUM(err) BY version",
    )
    return [
        f"{version}: {n_errors} errors in {lines} lines"
        for lines, n_errors, version in rows
        if (errors and n_errors) or (version and version not in healthy)
    ]


def healthy_versions(service: str) -> set[str]:
    """APP_VERSION in the healthy base manifests (git is the source of truth)."""
    manifest = (BASE_MANIFESTS / f"{service}.yaml").read_text()
    return set(re.findall(r'APP_VERSION:\s*"([^"]+)"', manifest))


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def task_for_window(scenario: str, start: datetime, end: datetime) -> AgentTask:
    """The scenario's question over an explicit window (live windows aren't fixed)."""
    task = SCENARIOS[scenario].task("logs", end)
    context = task.context.model_copy(update={"time_range": TimeRange(start=start, end=end)})
    return task.model_copy(update={"context": context})


def window_for(scenario: str, end: datetime, lead: timedelta) -> tuple[datetime, datetime]:
    """Incident: from ``lead`` before the injection (the engineer's 'since ~10 min before');
    healthy S0: the scenario's own window."""
    incident = os.environ.get("AIOPS_INCIDENT_START")
    if SCENARIOS[scenario].healthy or not incident:
        return SCENARIOS[scenario].task("logs", end).context.time_range.start, end
    start = datetime.fromisoformat(incident.replace("Z", "+00:00")) - lead
    return start.replace(microsecond=0), end


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
    parser.add_argument(
        "--lead-minutes",
        type=float,
        default=10.0,
        help="Incident window starts this long before the injection.",
    )
    parser.add_argument(
        "--check-only", action="store_true", help="Only report whether the windows are clean."
    )
    parser.add_argument(
        "--es-url", default=os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
    )
    args = parser.parse_args()
    scenario = args.scenario.upper()
    out: Path = args.out.resolve()
    service = SCENARIOS[scenario].service or "payment-service"
    baseline = timedelta(
        hours=float(load_settings(ENV, CONFIG).capability("logs").settings["baseline_hours"])
    )

    def run() -> None:
        last = wait_for_fresh_logs(args.es_url, service)
        end = datetime.now(UTC).replace(microsecond=0)
        start, end = window_for(scenario, end, timedelta(minutes=args.lead_minutes))
        # The shared cluster also runs other agents' scenarios: a fixture is only a proof
        # if nothing else touched this service in the window it is judged on.
        if SCENARIOS[scenario].healthy:
            dirty = pollution(args.es_url, service, start, end)
            where = f"window {iso(start)}..{iso(end)}"
        else:
            injected = start + timedelta(minutes=args.lead_minutes)
            # Baseline: no earlier run of an incident version (it would hide the deploy and
            # the new pattern); other scenarios' errors there are fine. Lead: fully healthy.
            dirty = pollution(args.es_url, service, start - baseline, start, errors=False)
            dirty += pollution(args.es_url, service, start, injected)
            where = f"before the injection {iso(start - baseline)}..{iso(injected)}"
        print(f"latest {service} log in ES: {last}; {where}: {dirty or 'clean'}")
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
