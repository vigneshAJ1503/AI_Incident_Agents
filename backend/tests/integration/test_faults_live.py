"""Live fault injection against the local Minikube cluster (slow: ~12 min).

Needs `make infra-up k8s-up`. Run with: make test-faults
Each scenario: inject -> let it develop -> assert the signature -> revert (always).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from aiops.faults import FaultInjector, FaultState, kubectl_runner

pytestmark = pytest.mark.faults
REPO = Path(__file__).resolve().parents[3]
kubectl = kubectl_runner("aiops")


def logs(deploy: str) -> str:
    return kubectl(("-n", "prod", "logs", f"deploy/{deploy}", "--since=150s", "--all-containers"))


def pods(app: str) -> list[dict[str, object]]:
    data = json.loads(kubectl(("-n", "prod", "get", "pods", "-l", f"app={app}", "-o", "json")))
    items: list[dict[str, object]] = data["items"]
    return items


def container_states(app: str) -> str:
    return json.dumps([p.get("status", {}) for p in pods(app)])


CHECKS: dict[str, Callable[[], None]] = {}


def check(scenario: str) -> Callable[[Callable[[], None]], Callable[[], None]]:
    def register(fn: Callable[[], None]) -> Callable[[], None]:
        CHECKS[scenario] = fn
        return fn

    return register


@check("S1")
def _s1() -> None:
    text = logs("payment-service")
    assert "Database connection timeout: could not acquire a connection" in text
    assert "(DB_POOL_SIZE=2," in kubectl(("-n", "prod", "logs", "deploy/payment-service"))


@check("S2")
def _s2() -> None:
    states = container_states("order-service")
    assert "OOMKilled" in states, states[:500]


@check("S3")
def _s3() -> None:
    assert "Timeout calling inventory-service" in logs("order-service")
    assert "Slow query on stock_levels" in logs("inventory-service")


@check("S4")
def _s4() -> None:
    states = container_states("user-service")
    assert "ImagePullBackOff" in states or "ErrImagePull" in states, states[:500]


@check("S5")
def _s5() -> None:
    assert "Redis connection refused" in logs("payment-service") + logs("user-service")


@pytest.mark.parametrize("scenario", ["S1", "S2", "S3", "S4", "S5"])
def test_fault_signature_live(scenario: str) -> None:
    injector = FaultInjector(REPO, kubectl, log=print)
    injector.run_scenario(scenario, lambda state: CHECKS[scenario]())
    assert injector.state() == FaultState()
