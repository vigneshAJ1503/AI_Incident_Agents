"""The last phase of an investigation: RCA (PR-033) + response builder (PR-034).

Publishes ``rca_started``, ``hypothesis_ranked`` and ``report_ready``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aiops.agents.rca_agent import RCAAgent
from aiops.core.prompts import PromptLoader
from aiops.orchestrator.response import build_report, build_timeline

if TYPE_CHECKING:
    from aiops.orchestrator.engine import InvestigationRun, Orchestrator


async def conclude(orchestrator: Orchestrator, run: InvestigationRun) -> None:
    inv = run.investigation
    if inv.context is None:
        return
    settings = orchestrator.settings
    orchestrator.bus.publish("rca_started", inv.id)
    use_llm = (
        settings.orchestrator.llm_rca
        and not orchestrator.deterministic_only
        and inv.usage.total_tokens < settings.orchestrator.max_tokens
    )
    prompts = PromptLoader(
        settings.config_dir / "prompts", overrides=settings.prompt_override_dirs()
    )
    agent = RCAAgent(orchestrator.llm if use_llm else None, prompts)
    service = orchestrator.catalog.resolve(inv.context.service or "").service
    outcome = await agent.analyze(
        inv.context,
        [r for r in inv.results],
        dependencies=list(service.depends_on) if service else [],
    )
    inv.usage = inv.usage + outcome.usage
    inv.hypotheses = outcome.hypotheses
    inv.recommendations = outcome.recommendations
    inv.claims = outcome.claims
    run.notes.extend(outcome.notes)
    orchestrator.bus.publish(
        "hypothesis_ranked",
        inv.id,
        hypotheses=[h.model_dump(mode="json") for h in outcome.hypotheses],
    )
    inv.timeline = build_timeline(inv)
    inv.versions = {**inv.versions, "rca": agent.version}
    inv.report = build_report(
        inv,
        outcome,
        run.missing,
        rules=settings.orchestrator.severity,
        tier=service.tier if service else None,
    )
    orchestrator.bus.publish("report_ready", inv.id, report=inv.report.model_dump(mode="json"))
