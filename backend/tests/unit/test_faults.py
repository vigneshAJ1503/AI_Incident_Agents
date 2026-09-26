from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from aiops.faults import FAULTS, FaultError, FaultInjector, FaultState, cluster_lock, resolve


class FakeKubectl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: Sequence[str]) -> str:
        self.calls.append(tuple(args))
        return ""

    def joined(self) -> list[str]:
        return [" ".join(c) for c in self.calls]


def injector(tmp_path: Path) -> tuple[FaultInjector, FakeKubectl, list[float]]:
    kubectl, slept = FakeKubectl(), []
    return FaultInjector(tmp_path, kubectl, sleep=slept.append, log=lambda _: None), kubectl, slept


def test_resolve_scenarios_and_types() -> None:
    assert resolve("S1").service == "payment-service"
    assert resolve("s3").scenario == "S3"
    assert resolve("db-timeout").scenario == "S1"
    assert resolve("cache-outage").scenario == "S5"
    with pytest.raises(FaultError, match="Unknown fault"):
        resolve("network-partition")


def test_s1_patches_pool_and_rolls_out_with_change_cause(tmp_path: Path) -> None:
    inj, kubectl, slept = injector(tmp_path)
    state = inj.inject("S1")
    calls = kubectl.joined()
    assert any(
        "patch configmap payment-service-config" in c and '"DB_POOL_SIZE": "2"' in c for c in calls
    )
    assert any("kubernetes.io/change-cause=v1.8.2: tune db pool" in c for c in calls)
    assert calls[-1] == "-n prod rollout restart deployment/payment-service"
    assert slept == [FAULTS["S1"].settle_s]
    assert state.scenario == "S1" and state.injected_at
    assert FaultState.load(tmp_path / ".data" / "fault-state.json").scenario == "S1"


@pytest.mark.parametrize(
    ("scenario", "needle"),
    [
        ("S2", '"LEAK_KB_PER_REQUEST": "1024"'),
        ("S3", '"SLOW_QUERY_MS": "3500"'),
        ("S4", "set image deployment/user-service app=aiops/sample-service:v3.2.0"),
        ("S5", "scale deployment/redis --replicas=0"),
    ],
)
def test_other_scenarios(tmp_path: Path, scenario: str, needle: str) -> None:
    inj, kubectl, _ = injector(tmp_path)
    inj.inject(scenario, wait=False)
    assert any(needle in c for c in kubectl.joined())


def test_only_one_scenario_at_a_time(tmp_path: Path) -> None:
    inj, _, _ = injector(tmp_path)
    inj.inject("S1", wait=False)
    with pytest.raises(FaultError, match="already active"):
        inj.inject("S5", wait=False)


def test_revert_reapplies_base_and_clears_state(tmp_path: Path) -> None:
    inj, kubectl, _ = injector(tmp_path)
    inj.inject("S5", wait=False)
    kubectl.calls.clear()
    inj.revert()
    calls = kubectl.joined()
    assert calls[0] == f"apply -k {tmp_path / 'deploy/k8s/base'}"
    assert "-n prod scale deployment/redis --replicas=1" in calls
    assert "-n prod rollout status deployment/payment-service --timeout=180s" in calls
    assert inj.state().scenario is None


def test_run_scenario_always_reverts(tmp_path: Path) -> None:
    inj, kubectl, _ = injector(tmp_path)
    seen: list[str | None] = []

    def boom(state: FaultState) -> None:
        seen.append(state.scenario)
        raise RuntimeError("recording failed")

    with pytest.raises(RuntimeError):
        inj.run_scenario("S1", boom)
    assert seen == ["S1"]
    assert any(c.startswith("apply -k") for c in kubectl.joined())
    assert inj.state().scenario is None


def test_busy_cluster_is_reported(tmp_path: Path) -> None:
    inj, _, _ = injector(tmp_path)
    with cluster_lock(inj.lock_path), pytest.raises(FaultError, match="busy"):
        inj.inject("S1", wait=False)
