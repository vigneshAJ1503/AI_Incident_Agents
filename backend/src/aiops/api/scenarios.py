"""Scenarios for the API: replay selection (``POST /investigations`` without a live stack)
and the opt-in fault injection endpoints (``AIOPS_ENABLE_FAULTS=1``).

Fault injection reuses ``aiops.faults`` as it is: the same injector, the same cluster lock
(``.data/cluster.lock`` of the repo the API runs from) and the same kubectl context.
"""

from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from aiops.core.catalog import ServiceCatalog
from aiops.evals.runner import EvalPaths
from aiops.evals.scenario import Scenario, load_scenarios
from aiops.faults import FAULTS, FaultError, FaultInjector, FaultState, kubectl_runner

FAULTS_ENV = "AIOPS_ENABLE_FAULTS"
KUBE_CONTEXT_ENV = "AIOPS_FAULTS_KUBE_CONTEXT"
_WORD = re.compile(r"[a-z0-9]+")
_STOP = {"the", "is", "a", "an", "in", "on", "of", "and", "or", "to", "why", "what", "are"}
#: Minimum word overlap for a question to select a scenario without a service.
MIN_SIMILARITY = 0.3


def faults_enabled() -> bool:
    return os.environ.get(FAULTS_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def faults_available(settings_auth: str) -> bool:
    """Fault endpoints are double-guarded (PR-042): the env flag AND API authentication."""
    return faults_enabled() and settings_auth != "none"


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.casefold()) if w not in _STOP}


def similarity(a: str, b: str) -> float:
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


@dataclass
class ScenarioCatalog:
    scenarios: list[Scenario] = field(default_factory=list)

    @classmethod
    def load(cls, config_dir: Path) -> ScenarioCatalog:
        root = EvalPaths.discover(config_dir).scenarios
        return cls(sorted(load_scenarios(root), key=lambda s: s.id) if root.is_dir() else [])

    def get(self, scenario_id: str) -> Scenario | None:
        return next((s for s in self.scenarios if s.id.upper() == scenario_id.upper()), None)

    def match(
        self, question: str, service: str | None, catalog: ServiceCatalog | None = None
    ) -> Scenario | None:
        """The scenario whose question fits best; a named/resolved service narrows the
        choice (a replay only has fixtures for its scenario's own service)."""
        normalized = " ".join(question.casefold().split())
        for s in self.scenarios:
            if " ".join(s.question.casefold().split()) == normalized:
                return s
        name = service
        if name and catalog is not None:
            resolved = catalog.resolve(name).service
            name = resolved.name if resolved else name
        if name is None and catalog is not None:
            resolved = catalog.resolve(question).service  # a service named in the question
            name = resolved.name if resolved else None

        def score(s: Scenario) -> float:
            return similarity(question, f"{s.question} {s.title} {s.description}")

        if name is not None:
            candidates = [s for s in self.scenarios if s.service == name]
            if candidates:
                return max(candidates, key=lambda s: (score(s), not s.healthy))
            return None
        best = max(self.scenarios, key=score, default=None)
        return best if best is not None and score(best) >= MIN_SIMILARITY else None


# --------------------------------------------------------------------------- fault injection


class FaultController(Protocol):
    #: Why the last background revert failed (None after a successful one).
    last_error: str | None

    def active(self) -> str | None: ...

    def reverting(self) -> bool: ...

    def inject(self, scenario_id: str) -> FaultState: ...

    def revert(self) -> None: ...


def injectable(scenario_id: str) -> bool:
    return scenario_id.upper() in FAULTS


class KubectlFaultController:
    """``aiops fault inject/revert`` for the API: inject without waiting (the UI shows the
    incident developing), revert in a background thread (rollouts take minutes)."""

    def __init__(
        self,
        repo_root: Path,
        *,
        context: str | None = None,
        injector_factory: Callable[[], FaultInjector] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.context = context or os.environ.get(KUBE_CONTEXT_ENV, "aiops")
        self._factory = injector_factory or (
            lambda: FaultInjector(self.repo_root, kubectl_runner(self.context), log=lambda _: None)
        )
        self._reverting: threading.Thread | None = None
        self.last_error: str | None = None

    def active(self) -> str | None:
        return self._factory().state().scenario

    def inject(self, scenario_id: str) -> FaultState:
        return self._factory().inject(scenario_id, wait=False)

    def reverting(self) -> bool:
        return self._reverting is not None and self._reverting.is_alive()

    def revert(self) -> None:
        if self.reverting():
            raise FaultError("A revert is already in progress.")
        self.last_error = None  # a new attempt: the UI waits for this one's outcome

        def run() -> None:
            try:
                self._factory().revert()
                self.last_error = None
            except FaultError as exc:
                self.last_error = str(exc)

        self._reverting = threading.Thread(target=run, name="aiops-fault-revert", daemon=True)
        self._reverting.start()
