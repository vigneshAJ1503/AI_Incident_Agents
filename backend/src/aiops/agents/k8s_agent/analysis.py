"""Deterministic Kubernetes workload analysis (UC-07).

Turns kubernetes-mcp results (deployment + rollout history, pods, Warning events,
dependency deployments) into facts and signals. The LLM reasons over these; it
doesn't parse container states, compare timestamps or decide what counts as a restart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from aiops.core.models import TimeRange

#: Signal vocabulary (must match config/prompts/k8s/v*.md). All are data-derived.
PROBLEM_SIGNALS = (
    "pod_restarts",
    "oom_killed",
    "crash_loop",
    "image_pull_error",
    "replicas_unavailable",
    "probe_failures",
    "dependency_unavailable",
)
CHANGE_SIGNALS = ("recent_rollout", "dependency_rollout")
SIGNALS = (*PROBLEM_SIGNALS, *CHANGE_SIGNALS, "healthy")

IMAGE_PULL_REASONS = frozenset(
    {"ImagePullBackOff", "ErrImagePull", "InvalidImageName", "ErrImageNeverPull"}
)
CRASH_REASONS = frozenset({"CrashLoopBackOff", "RunContainerError", "CreateContainerError"})
EVENTS_NOTE = "Kubernetes keeps events for about 1 hour: older events are not visible."
ROOT_CAUSE_NOTE = (
    "Kubernetes shows symptoms and changes (restarts, OOM kills, rollouts). A rollout that "
    "coincides with the incident is a CORRELATION; only call it a cause with other evidence."
)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


def iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def offset(ts: datetime, anchor: datetime) -> str:
    delta = (ts - anchor).total_seconds()
    sign = "+" if delta >= 0 else "-"
    delta = abs(delta)
    return f"{sign}{delta / 60:.0f}m" if delta >= 90 else f"{sign}{delta:.0f}s"


def _images(items: Any) -> list[str]:
    return [str(i.get("image")) for i in items or [] if isinstance(i, dict)]


# --------------------------------------------------------------------------- views


@dataclass(frozen=True)
class Revision:
    revision: int | None
    images: list[str]
    change_cause: str | None
    created_at: datetime | None
    current: bool
    ready: int
    desired: int


@dataclass(frozen=True)
class Rollout:
    """A revision created inside the window, compared with the one before it."""

    deployment: str
    revision: Revision
    previous: Revision | None

    @property
    def image_changed(self) -> bool:
        return self.previous is not None and self.previous.images != self.revision.images

    def line(self, anchor: datetime) -> str:
        rev = self.revision
        when = (
            f"{iso(rev.created_at)} ({offset(rev.created_at, anchor)} vs incident start)"
            if rev.created_at
            else "at an unknown time"
        )
        cause = f"change-cause '{rev.change_cause}'" if rev.change_cause else "no change-cause"
        if self.previous is None:
            images = f"image {', '.join(rev.images)}"
        elif self.image_changed:
            images = f"image {', '.join(self.previous.images)} -> {', '.join(rev.images)}"
        else:
            images = f"same image as revision {self.previous.revision} ({', '.join(rev.images)}): a config/restart rollout"
        state = "current" if rev.current else "not the active revision"
        return (
            f"  - {self.deployment} revision {rev.revision} rolled out {when}, {cause}, {images}; "
            f"{rev.ready}/{rev.desired} of its pods ready ({state})"
        )


@dataclass(frozen=True)
class ContainerView:
    pod: str
    name: str
    image: str
    ready: bool
    restart_count: int
    waiting_reason: str | None
    waiting_message: str | None
    last_reason: str | None
    last_exit_code: int | None
    last_finished_at: datetime | None


@dataclass(frozen=True)
class PodView:
    name: str
    phase: str
    ready: str
    created_at: datetime | None
    deleting: bool
    containers: list[ContainerView]


@dataclass(frozen=True)
class EventView:
    type: str
    reason: str
    object: str
    message: str
    count: int
    first_seen: datetime | None
    last_seen: datetime | None

    def line(self, anchor: datetime) -> str:
        when = (
            f"{iso(self.last_seen)} ({offset(self.last_seen, anchor)})" if self.last_seen else "?"
        )
        return f"  - {self.reason} x{self.count} on {self.object}, last {when}: {self.message}"


@dataclass(frozen=True)
class DependencyView:
    name: str
    found: bool
    desired: int = 0
    ready: int = 0
    revision: str | None = None
    change_cause: str | None = None
    last_rollout: datetime | None = None

    @property
    def unavailable(self) -> bool:
        return self.found and (self.desired == 0 or self.ready < self.desired)

    def state(self) -> str:
        if not self.found:
            return f"{self.name}: no deployment of that name (not a Kubernetes workload here?)"
        if self.desired == 0:
            return f"{self.name}: SCALED TO 0 replicas (0/0 ready): nothing can serve it"
        return f"{self.name}: {self.ready}/{self.desired} ready"


def revisions(data: Any) -> list[Revision]:
    history = data.get("rollout_history", []) if isinstance(data, dict) else []
    out = []
    for h in history:
        if not isinstance(h, dict):
            continue
        replicas = h.get("replicas") or {}
        out.append(
            Revision(
                revision=h.get("revision"),
                images=_images(h.get("images")),
                change_cause=h.get("change_cause"),
                created_at=parse_ts(h.get("created_at")),
                current=bool(h.get("current")),
                ready=int(replicas.get("ready") or 0),
                desired=int(replicas.get("desired") or 0),
            )
        )
    return sorted(out, key=lambda r: r.revision or 0, reverse=True)


def pod_views(data: Any) -> list[PodView]:
    pods = data.get("pods", []) if isinstance(data, dict) else []
    views = []
    for p in pods:
        if not isinstance(p, dict):
            continue
        containers = []
        for c in p.get("containers") or []:
            state, last = c.get("state") or {}, c.get("last_state") or {}
            waiting = state.get("state") == "waiting"
            terminated_now = state.get("state") == "terminated"
            term = state if terminated_now else last if last.get("state") == "terminated" else {}
            containers.append(
                ContainerView(
                    pod=str(p.get("name")),
                    name=str(c.get("name")),
                    image=str(c.get("image")),
                    ready=bool(c.get("ready")),
                    restart_count=int(c.get("restart_count") or 0),
                    waiting_reason=state.get("reason") if waiting else None,
                    waiting_message=state.get("message") if waiting else None,
                    last_reason=term.get("reason"),
                    last_exit_code=term.get("exit_code"),
                    last_finished_at=parse_ts(term.get("finished_at")),
                )
            )
        views.append(
            PodView(
                name=str(p.get("name")),
                phase=str(p.get("phase")),
                ready=str(p.get("ready")),
                created_at=parse_ts(p.get("created_at")),
                deleting=bool(p.get("deleting")),
                containers=containers,
            )
        )
    return views


def event_views(data: Any) -> list[EventView]:
    events = data.get("events", []) if isinstance(data, dict) else []
    return [
        EventView(
            type=str(e.get("type")),
            reason=str(e.get("reason")),
            object=str(e.get("object")),
            message=str(e.get("message") or ""),
            count=int(e.get("count") or 1),
            first_seen=parse_ts(e.get("first_seen")),
            last_seen=parse_ts(e.get("last_seen")),
        )
        for e in events
        if isinstance(e, dict)
    ]


def _last_rollout(dep: dict[str, Any]) -> datetime | None:
    """When the deployment's latest rollout completed (Progressing=NewReplicaSetAvailable)."""
    for cond in dep.get("conditions") or []:
        if cond.get("type") == "Progressing" and cond.get("reason") == "NewReplicaSetAvailable":
            return parse_ts(cond.get("last_update"))
    return None


def dependency_views(data: Any, wanted: dict[str, str]) -> list[DependencyView]:
    """``wanted`` maps dependency name -> deployment name."""
    deployments = data.get("deployments", []) if isinstance(data, dict) else []
    by_name = {str(d.get("name")): d for d in deployments if isinstance(d, dict)}
    views = []
    for name, deployment in wanted.items():
        dep = by_name.get(deployment)
        if dep is None:
            views.append(DependencyView(name=name, found=False))
            continue
        replicas = dep.get("replicas") or {}
        views.append(
            DependencyView(
                name=name,
                found=True,
                desired=int(replicas.get("desired") or 0),
                ready=int(replicas.get("ready") or 0),
                revision=dep.get("revision"),
                change_cause=dep.get("change_cause"),
                last_rollout=_last_rollout(dep),
            )
        )
    return views


# --------------------------------------------------------------------------- analysis


@dataclass
class K8sAnalysis:
    service: str
    deployment: str
    namespace: str
    window: TimeRange
    incident_start: datetime
    incident_start_source: str
    probe_startup_grace: timedelta
    found: bool = True
    desired: int = 0
    ready: int = 0
    available: int = 0
    unavailable: int = 0
    updated: int = 0
    revision: str | None = None
    change_cause: str | None = None
    images: list[str] = field(default_factory=list)
    conditions: list[dict[str, Any]] = field(default_factory=list)
    history: list[Revision] = field(default_factory=list)
    pods: list[PodView] = field(default_factory=list)
    events: list[EventView] = field(default_factory=list)
    dependencies: list[DependencyView] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # parts that could not be queried

    # -- window helpers ------------------------------------------------------------------

    def in_window(self, ts: datetime | None) -> bool:
        return ts is not None and self.window.start <= ts <= self.window.end

    def _recent(self, ts: datetime | None) -> bool:
        """In the window, or unknown (a current state without a timestamp)."""
        return ts is None or ts >= self.window.start

    # -- facts ---------------------------------------------------------------------------

    @property
    def rollouts(self) -> list[Rollout]:
        out = []
        for i, rev in enumerate(self.history):
            if self.in_window(rev.created_at):
                previous = self.history[i + 1] if i + 1 < len(self.history) else None
                out.append(Rollout(self.deployment, rev, previous))
        return out

    @property
    def containers(self) -> list[ContainerView]:
        return [c for p in self.pods for c in p.containers]

    @property
    def restarted(self) -> list[ContainerView]:
        return [
            c for c in self.containers if c.restart_count > 0 and self._recent(c.last_finished_at)
        ]

    @property
    def oom_killed(self) -> list[ContainerView]:
        return [
            c
            for c in self.containers
            if c.last_reason == "OOMKilled" and self._recent(c.last_finished_at)
        ]

    def _warnings(self, *reasons: str) -> list[EventView]:
        return [e for e in self.events if e.type == "Warning" and e.reason in reasons]

    @property
    def oom_events(self) -> list[EventView]:
        return [
            e
            for e in self.events
            if e.type == "Warning"
            and (e.reason in ("OOMKilling", "OOMKilled") or "OOMKilled" in e.message)
        ]

    @property
    def crash_looping(self) -> list[ContainerView]:
        return [c for c in self.containers if c.waiting_reason in CRASH_REASONS]

    @property
    def backoff_events(self) -> list[EventView]:
        return [e for e in self._warnings("BackOff") if "restarting failed container" in e.message]

    @property
    def pull_errors(self) -> list[ContainerView]:
        return [c for c in self.containers if c.waiting_reason in IMAGE_PULL_REASONS]

    @property
    def pull_events(self) -> list[EventView]:
        return [
            e
            for e in self.events
            if e.type == "Warning"
            and (
                e.reason in IMAGE_PULL_REASONS
                or (e.reason in ("Failed", "BackOff") and "pull" in e.message.casefold())
                or any(r in e.message for r in IMAGE_PULL_REASONS)
            )
        ]

    @property
    def probe_events(self) -> list[EventView]:
        """Probe failures on the pods serving now, minus rollout noise.

        Normal rollouts produce readiness failures that mean nothing: a new container
        is not ready for a few seconds after it starts (short span), and a replaced pod
        fails its probes while it shuts down (it no longer exists or is terminating).
        """
        live = {f"Pod/{p.name}" for p in self.pods if not p.deleting}
        failures = []
        for e in self._warnings("Unhealthy"):
            if e.object not in live:
                continue
            liveness = "liveness" in e.message.casefold()
            span = (e.last_seen - e.first_seen) if e.last_seen and e.first_seen else timedelta(0)
            if liveness or span > self.probe_startup_grace:
                failures.append(e)
        return failures

    @property
    def replicas_unavailable(self) -> bool:
        return (
            self.found
            and self.desired > 0
            and (
                self.ready < self.desired
                or self.available < self.desired
                or self.unavailable > 0  # e.g. new pods stuck while the old ReplicaSet serves
            )
        )

    @property
    def unavailable_dependencies(self) -> list[DependencyView]:
        return [d for d in self.dependencies if d.unavailable]

    @property
    def dependency_rollouts(self) -> list[DependencyView]:
        return [d for d in self.dependencies if d.found and self.in_window(d.last_rollout)]

    @property
    def signals(self) -> list[str]:
        found: set[str] = set()
        if self.restarted:
            found.add("pod_restarts")
        if self.oom_killed or self.oom_events:
            found.add("oom_killed")
        if self.crash_looping or self.backoff_events:
            found.add("crash_loop")
        if self.pull_errors or self.pull_events:
            found.add("image_pull_error")
        if self.replicas_unavailable:
            found.add("replicas_unavailable")
        if self.probe_events:
            found.add("probe_failures")
        if self.unavailable_dependencies:
            found.add("dependency_unavailable")
        if self.rollouts:
            found.add("recent_rollout")
        if self.dependency_rollouts:
            found.add("dependency_rollout")
        if self.found and not self.failed and not found & set(PROBLEM_SIGNALS):
            found.add("healthy")
        return [s for s in SIGNALS if s in found]

    @property
    def problem(self) -> bool:
        """Anything beyond 'healthy' (a problem or a change worth reporting)."""
        return any(s != "healthy" for s in self.signals)

    # -- text ----------------------------------------------------------------------------

    def headline(self) -> str:
        """One data-derived sentence that always leads the summary."""
        if not self.found:
            return f"Deployment {self.namespace}/{self.deployment} was not found."
        parts = [
            f"{self.deployment}: {self.ready}/{self.desired} replicas ready"
            + (
                f" ({self.unavailable} unavailable, {self.updated}/{self.desired} updated)"
                if self.unavailable
                else ""
            )
            + (f", {len(self.restarted)} container(s) restarted" if self.restarted else "")
        ]
        if self.oom_killed or self.oom_events:
            parts.append("OOMKilled")
        if self.crash_looping:
            parts.append("CrashLoopBackOff")
        if self.pull_errors or self.pull_events:
            reasons = sorted({c.waiting_reason for c in self.pull_errors if c.waiting_reason})
            parts.append(" / ".join(reasons) or "image pull errors")
        for r in self.rollouts:
            cause = f" ({r.revision.change_cause})" if r.revision.change_cause else ""
            when = f" at {iso(r.revision.created_at)}" if r.revision.created_at else ""
            parts.append(f"rolled out revision {r.revision.revision}{cause}{when}")
        for d in self.unavailable_dependencies:
            parts.append(f"dependency {d.state()}")
        for d in self.dependency_rollouts:
            cause = f" ({d.change_cause})" if d.change_cause else ""
            when = f" at {iso(d.last_rollout)}" if d.last_rollout else ""
            parts.append(f"dependency {d.name} rolled out revision {d.revision}{cause}{when}")
        if "healthy" in self.signals and not self.problem:
            parts.append("no restarts, no Warning events, nothing abnormal in Kubernetes")
        return "; ".join(parts) + "."

    def lines(self) -> list[str]:
        w = self.window
        out = [
            self.headline(),
            f"Incident window: {iso(w.start)} to {iso(w.end)}. Incident start used for "
            f"correlation: {iso(self.incident_start)} ({self.incident_start_source}).",
        ]
        if not self.found:
            out.append(f"Deployment {self.namespace}/{self.deployment} does not exist.")
        else:
            out.append(
                f"Deployment {self.namespace}/{self.deployment}: desired {self.desired}, updated "
                f"{self.updated}, ready {self.ready}, available {self.available}; revision "
                f"{self.revision}, change-cause '{self.change_cause or '-'}', image(s) "
                f"{', '.join(self.images) or '?'}."
            )
            bad = [
                cond
                for cond in self.conditions
                if cond.get("status") != "True" or cond.get("reason") == "ProgressDeadlineExceeded"
            ]
            for cond in bad:
                out.append(
                    f"  condition {cond.get('type')}={cond.get('status')} ({cond.get('reason')}): {cond.get('message')}"
                )
        if self.rollouts:
            out.append(f"Rollouts inside the window ({len(self.rollouts)}):")
            out.extend(r.line(self.incident_start) for r in self.rollouts)
        elif self.history:
            last = self.history[0]
            when = iso(last.created_at) if last.created_at else "?"
            out.append(
                f"No rollout inside the window; latest revision {last.revision} "
                f"('{last.change_cause or '-'}') was created {when}."
            )
        if self.pods:
            out.append(f"Pods ({len(self.pods)}):")
            for p in self.pods:
                details = []
                for c in p.containers:
                    bits = [f"{c.name} restarts={c.restart_count}"]
                    if c.waiting_reason:
                        bits.append(f"waiting {c.waiting_reason}")
                    if c.last_reason:
                        when = iso(c.last_finished_at) if c.last_finished_at else "?"
                        bits.append(
                            f"last terminated {c.last_reason} (exit {c.last_exit_code}) at {when}"
                        )
                    details.append(" ".join(bits))
                flag = " (terminating)" if p.deleting else ""
                out.append(f"  - {p.name} {p.phase} ready {p.ready}{flag}: {'; '.join(details)}")
        elif self.found:
            out.append("No pods match the deployment's selector.")
        warnings = [e for e in self.events if e.type == "Warning"]
        if warnings:
            out.append(f"Warning events in the window ({len(warnings)}):")
            out.extend(e.line(self.incident_start) for e in warnings[-12:])
            noise = len(self._warnings("Unhealthy")) - len(self.probe_events)
            if noise:
                out.append(
                    f"  ({noise} readiness failure(s) are rollout noise: new containers not "
                    "ready for a few seconds after starting, or replaced pods shutting down.)"
                )
        else:
            out.append("No Warning events for the deployment's objects in the window.")
        if self.dependencies:
            out.append("Dependencies: " + "; ".join(d.state() for d in self.dependencies) + ".")
            for d in self.dependency_rollouts:
                out.append(
                    f"  - dependency {d.name} finished a rollout (revision {d.revision}, "
                    f"'{d.change_cause or '-'}') at {iso(d.last_rollout) if d.last_rollout else '?'}, inside the window"
                )
        if self.failed:
            out.append(f"Could not query: {', '.join(self.failed)}.")
        out.append(EVENTS_NOTE)
        out.append(ROOT_CAUSE_NOTE)
        out.append(f"Deterministic signals: {', '.join(self.signals) or 'none'}")
        return out


def analyze(
    deployment_data: Any,
    pods_data: Any,
    events_data: Any,
    dependencies_data: Any,
    *,
    service: str,
    deployment: str,
    namespace: str,
    dependencies: dict[str, str],
    window: TimeRange,
    incident_start: datetime,
    incident_start_source: str,
    probe_startup_grace_s: float = 60.0,
    failed: list[str] | None = None,
) -> K8sAnalysis:
    dep = deployment_data.get("deployment") if isinstance(deployment_data, dict) else None
    dep = dep if isinstance(dep, dict) else None
    replicas = (dep or {}).get("replicas") or {}
    events = [
        e for e in event_views(events_data) if e.last_seen is None or e.last_seen >= window.start
    ]
    return K8sAnalysis(
        service=service,
        deployment=deployment,
        namespace=namespace,
        window=window,
        incident_start=incident_start,
        incident_start_source=incident_start_source,
        probe_startup_grace=timedelta(seconds=probe_startup_grace_s),
        found=dep is not None,
        desired=int(replicas.get("desired") or 0),
        ready=int(replicas.get("ready") or 0),
        available=int(replicas.get("available") or 0),
        unavailable=int(replicas.get("unavailable") or 0),
        updated=int(replicas.get("updated") or 0),
        revision=(dep or {}).get("revision"),
        change_cause=(dep or {}).get("change_cause"),
        images=_images((dep or {}).get("images")),
        conditions=list((dep or {}).get("conditions") or []),
        history=revisions(deployment_data),
        pods=pod_views(pods_data),
        events=events,
        dependencies=dependency_views(dependencies_data, dependencies)
        if dependencies_data is not None
        else [],
        failed=list(failed or []),
    )
