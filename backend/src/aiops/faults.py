"""Live fault injection into the local Kubernetes sample system (PR-017).

Each scenario is a small, reversible set of kubectl changes to the sample services
(`deploy/k8s/base`). Reverting re-applies the base manifests, so the healthy state
is always "whatever is in git".

The cluster is shared (other agents, evals, fixture recording), so every mutating
operation takes an exclusive file lock (`.data/cluster.lock`). `run_scenario()`
holds it across inject -> wait for the incident to be visible -> run -> revert.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NAMESPACE = "prod"
BASE_MANIFESTS = "deploy/k8s/base"
KUBECTL_TIMEOUT_S = 120


class FaultError(Exception):
    pass


Runner = Callable[[Sequence[str]], str]


def kubectl_runner(context: str) -> Runner:
    def run(args: Sequence[str]) -> str:
        argv = ["kubectl", "--context", context, *args]
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                argv, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FaultError(f"kubectl failed: {exc}") from exc
        if done.returncode != 0:
            raise FaultError(f"{' '.join(argv[3:])}: {done.stderr.strip() or done.stdout.strip()}")
        return done.stdout

    return run


@dataclass(frozen=True)
class Step:
    """One kubectl invocation (args after `kubectl --context X`)."""

    args: tuple[str, ...]
    description: str


def _patch_config(deploy: str, data: dict[str, str]) -> Step:
    return Step(
        (
            "-n",
            NAMESPACE,
            "patch",
            "configmap",
            f"{deploy}-config",
            "--type",
            "merge",
            "-p",
            json.dumps({"data": data}),
        ),
        f"set {', '.join(f'{k}={v}' for k, v in data.items())} on {deploy}",
    )


def _restart(deploy: str, cause: str) -> list[Step]:
    return [
        Step(
            (
                "-n",
                NAMESPACE,
                "annotate",
                "deployment",
                deploy,
                "--overwrite",
                f"kubernetes.io/change-cause={cause}",
            ),
            f"record change-cause on {deploy}",
        ),
        Step(("-n", NAMESPACE, "rollout", "restart", f"deployment/{deploy}"), f"roll out {deploy}"),
    ]


@dataclass(frozen=True)
class Fault:
    scenario: str
    name: str
    service: str
    description: str
    steps: tuple[Step, ...]
    #: Seconds to let the incident develop before it is considered visible.
    settle_s: int
    #: Deployments touched (reverted by re-applying the base manifests + restart).
    touched: tuple[str, ...] = field(default=())


FAULTS: dict[str, Fault] = {
    "S1": Fault(
        scenario="S1",
        name="db-timeout",
        service="payment-service",
        description="payment-service v1.8.2 with DB_POOL_SIZE=2: pool exhaustion -> DB timeouts, HTTP 500",
        steps=(
            _patch_config("payment-service", {"DB_POOL_SIZE": "2", "APP_VERSION": "v1.8.2"}),
            *_restart("payment-service", "v1.8.2: tune db pool (DB_POOL_SIZE 20 -> 2)"),
        ),
        settle_s=90,
        touched=("payment-service",),
    ),
    "S2": Fault(
        scenario="S2",
        name="memory-leak",
        service="order-service",
        description="order-service leaks 1 MB per order until it is OOMKilled and restarts",
        steps=(
            _patch_config("order-service", {"LEAK_KB_PER_REQUEST": "1024"}),
            *_restart("order-service", "v2.3.0: enable in-memory order cache"),
        ),
        settle_s=180,
        touched=("order-service",),
    ),
    "S3": Fault(
        scenario="S3",
        name="slow-dependency",
        service="order-service",
        description="inventory-service stock query takes 3.5 s: order-service upstream timeouts (504)",
        steps=(
            _patch_config("inventory-service", {"SLOW_QUERY_MS": "3500", "DB_POOL_SIZE": "40"}),
            *_restart("inventory-service", "v1.4.2: stock query without index"),
        ),
        settle_s=90,
        touched=("inventory-service",),
    ),
    "S4": Fault(
        scenario="S4",
        name="bad-deployment",
        service="user-service",
        description="user-service rolled out with a nonexistent image tag: ImagePullBackOff, replicas mismatch",
        steps=(
            Step(
                (
                    "-n",
                    NAMESPACE,
                    "set",
                    "image",
                    "deployment/user-service",
                    "app=aiops/sample-service:v3.2.0",
                ),
                "set user-service image to the nonexistent tag v3.2.0",
            ),
            Step(
                ("-n", NAMESPACE, "scale", "deployment/user-service", "--replicas=3"),
                "scale user-service to 3 replicas",
            ),
            Step(
                (
                    "-n",
                    NAMESPACE,
                    "annotate",
                    "deployment",
                    "user-service",
                    "--overwrite",
                    "kubernetes.io/change-cause=v3.2.0: bump image",
                ),
                "record change-cause on user-service",
            ),
        ),
        settle_s=90,
        touched=("user-service",),
    ),
    "S5": Fault(
        scenario="S5",
        name="cache-outage",
        service="payment-service",
        description="Redis scaled to 0: cache connection refused everywhere, database fallback",
        steps=(
            Step(
                ("-n", NAMESPACE, "scale", "deployment/redis", "--replicas=0"), "scale redis to 0"
            ),
        ),
        settle_s=90,
        touched=("redis",),
    ),
}

#: MASTER_PLAN fault TYPE names -> scenario.
FAULT_TYPES = {
    "db-timeout": "S1",
    "memory-leak": "S2",
    "crash-loop": "S2",
    "high-latency": "S3",
    "dependency-failure": "S3",
    "bad-config": "S4",
    "bad-deployment": "S4",
    "cache-outage": "S5",
}

SERVICE_DEPLOYMENTS = ("payment-service", "order-service", "user-service", "inventory-service")


def resolve(scenario_or_type: str) -> Fault:
    key = FAULT_TYPES.get(scenario_or_type, scenario_or_type).upper()
    if key not in FAULTS:
        known = ", ".join([*FAULTS, *FAULT_TYPES])
        raise FaultError(f"Unknown fault '{scenario_or_type}' (known: {known})")
    return FAULTS[key]


# --------------------------------------------------------------------------- state + lock


@dataclass
class FaultState:
    scenario: str | None = None
    injected_at: str | None = None

    @classmethod
    def load(cls, path: Path) -> FaultState:
        if not path.is_file():
            return cls()
        data: dict[str, Any] = json.loads(path.read_text())
        return cls(scenario=data.get("scenario"), injected_at=data.get("injected_at"))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"scenario": self.scenario, "injected_at": self.injected_at}))


@contextmanager
def cluster_lock(path: Path, wait: bool = True) -> Iterator[None]:
    """Exclusive lock so only one scenario is live at a time (across processes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        flags = fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle, flags)
        except BlockingIOError:
            raise FaultError(
                f"The cluster is busy (another scenario holds {path}). Try again later."
            ) from None
        try:
            handle.write(str(os.getpid()))
            handle.flush()
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# --------------------------------------------------------------------------- injector


class FaultInjector:
    def __init__(
        self,
        repo_root: Path,
        runner: Runner,
        *,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = print,
    ) -> None:
        self.repo_root = repo_root
        self.run = runner
        self.sleep = sleep
        self.log = log
        self.state_path = repo_root / ".data" / "fault-state.json"
        self.lock_path = repo_root / ".data" / "cluster.lock"

    def state(self) -> FaultState:
        return FaultState.load(self.state_path)

    # -- primitives (callers hold the lock) ------------------------------------------------

    def _inject(self, fault: Fault) -> FaultState:
        current = self.state()
        if current.scenario:
            raise FaultError(
                f"Scenario {current.scenario} is already active; run `make revert-fault` first."
            )
        for step in fault.steps:
            self.log(f"  - {step.description}")
            self.run(step.args)
        state = FaultState(
            scenario=fault.scenario,
            injected_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        state.save(self.state_path)
        return state

    def _revert(self) -> None:
        self.log("  - re-apply base manifests (ConfigMaps, images, replicas)")
        self.run(("apply", "-k", str(self.repo_root / BASE_MANIFESTS)))
        self.run(("-n", NAMESPACE, "scale", "deployment/redis", "--replicas=1"))
        for deploy in SERVICE_DEPLOYMENTS:
            self.run(
                (
                    "-n",
                    NAMESPACE,
                    "annotate",
                    "deployment",
                    deploy,
                    "--overwrite",
                    "kubernetes.io/change-cause=revert to baseline",
                )
            )
            self.run(("-n", NAMESPACE, "rollout", "restart", f"deployment/{deploy}"))
        for deploy in ("redis", *SERVICE_DEPLOYMENTS):
            self.run(
                ("-n", NAMESPACE, "rollout", "status", f"deployment/{deploy}", "--timeout=180s")
            )
        FaultState().save(self.state_path)

    # -- public API --------------------------------------------------------------------------

    def inject(self, name: str, *, wait: bool = True) -> FaultState:
        fault = resolve(name)
        with cluster_lock(self.lock_path, wait=False):
            self.log(f"Injecting {fault.scenario} ({fault.name}): {fault.description}")
            state = self._inject(fault)
            if wait:
                self.log(f"  - letting the incident develop for {fault.settle_s}s")
                self.sleep(fault.settle_s)
            return state

    def revert(self) -> None:
        with cluster_lock(self.lock_path, wait=False):
            self.log("Reverting to the healthy baseline")
            self._revert()

    def run_scenario(self, name: str, action: Callable[[FaultState], None]) -> FaultState:
        """inject -> wait -> action(state) -> revert, holding the lock throughout."""
        fault = resolve(name)
        with cluster_lock(self.lock_path, wait=True):
            self.log(f"Scenario {fault.scenario} ({fault.name}): {fault.description}")
            state = self._inject(fault)
            try:
                self.log(f"  - letting the incident develop for {fault.settle_s}s")
                self.sleep(fault.settle_s)
                action(state)
            finally:
                self.log("Reverting to the healthy baseline")
                self._revert()
            return state
