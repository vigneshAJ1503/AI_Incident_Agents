"""Code Agent (UC-08): recent code and config changes that could explain an incident.

Investigation loop:
  1. deterministic: release tags + commits touching the service's paths (and, weighted
     lower, its catalog dependencies' paths) in [incident start - lookback, incident end];
  2. deterministic: pre-rank commits by the files they touch, fetch the diffs of the top
     suspects, apply risk rules (config keys, image tags, dependencies, migrations,
     unbounded caches) weighted by closeness to the incident start; the relevant diff
     hunks become evidence;
  3. bounded LLM follow-ups + an evidence-cited report;
  4. deterministic signals and status are authoritative (the LLM can't add or drop them).

Vendor-neutral: repo names and paths from the service catalog (``code: {repo, paths}``);
tag scheme, thresholds and the commit link template from ``capabilities.code.settings``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent
from aiops.agents.code_agent.analysis import (
    DEFAULT_RISKY_KEYS,
    DEPENDENCY_WEIGHT,
    SIGNALS,
    CodeAnalysis,
    Commit,
    CommitRisk,
    DiffRules,
    Release,
    classify_path,
    parse_releases,
    proximity,
)
from aiops.agents.registry import AGENTS
from aiops.core.config import ConfigError
from aiops.core.models import AgentResult, AgentStatus, Evidence, EvidenceKind
from aiops.llm.base import ToolSpec

DEFAULT_LOOKBACK_HOURS = 24.0
DEFAULT_TAG_TEMPLATE = "{service}/{version}"
DEFAULT_MAX_DIFFS = 3
DEFAULT_THRESHOLD = 0.3
DEFAULT_MAX_COMMITS = 100


def iso(ts: datetime) -> str:
    return ts.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CodeTarget:
    service: str
    repo: str
    paths: tuple[str, ...]
    dependency: bool

    def owns(self, path: str) -> bool:
        return any(path == p or path.startswith(p.rstrip("/") + "/") for p in self.paths)


@dataclass(frozen=True)
class CodeScope:
    service: str
    targets: tuple[CodeTarget, ...]
    lookback: timedelta
    tag_template: str
    max_diffs: int
    threshold: float
    max_commits: int
    risky_keys: str
    link_template: str | None

    @property
    def primary(self) -> CodeTarget:
        return self.targets[0]

    def repos(self) -> dict[str, list[CodeTarget]]:
        grouped: dict[str, list[CodeTarget]] = {}
        for target in self.targets:
            grouped.setdefault(target.repo, []).append(target)
        return grouped


class CodeAgent(BaseAgent):
    spec = AgentSpec(
        name="code",
        version="1",
        description="Finds recent commits, config and deployment changes that could explain an incident.",
        capabilities=["code"],
        evidence_kind=EvidenceKind.COMMIT,
        prompt="code",
    )

    # -- scope ---------------------------------------------------------------------------

    def _target(self, service: str, environment: str | None, dependency: bool) -> CodeTarget | None:
        ids = self.deps.catalog.get(service).identifiers("code", environment)
        repo, paths = ids.get("repo"), ids.get("paths") or []
        if not repo or not paths:
            return None
        return CodeTarget(service, str(repo), tuple(str(p) for p in paths), dependency)

    def scope(self, run: AgentRun) -> CodeScope:
        ctx = run.task.context
        settings = self.deps.settings.capability("code").settings
        if not ctx.service:
            raise LookupError("No service given: the Code agent needs a service from the catalog.")
        primary = self._target(ctx.service, ctx.environment, dependency=False)
        if primary is None:
            raise LookupError(
                f"No code repository configured for service '{ctx.service}'. "
                "Add code: {repo, paths} to the service catalog."
            )
        targets = [primary]
        if settings.get("include_dependencies", True):
            for dep in self.deps.catalog.get(ctx.service).depends_on:
                try:
                    target = self._target(dep, ctx.environment, dependency=True)
                except ConfigError:  # infrastructure (postgres, redis) isn't a catalog service
                    continue
                if target is not None:
                    targets.append(target)
        link = settings.get("ui_link_template") or None
        return CodeScope(
            service=ctx.service,
            targets=tuple(targets),
            lookback=timedelta(hours=float(settings.get("lookback_hours", DEFAULT_LOOKBACK_HOURS))),
            tag_template=str(settings.get("release_tag_template", DEFAULT_TAG_TEMPLATE)),
            max_diffs=int(settings.get("max_diffs", DEFAULT_MAX_DIFFS)),
            threshold=float(settings.get("suspect_threshold", DEFAULT_THRESHOLD)),
            max_commits=int(settings.get("max_commits", DEFAULT_MAX_COMMITS)),
            risky_keys=str(settings.get("risky_keys", DEFAULT_RISKY_KEYS)),
            link_template=str(link) if link else None,
        )

    def commit_link(self, scope: CodeScope, repo: str, sha: str) -> str | None:
        if not scope.link_template:
            return None
        return scope.link_template.format(repo=repo, sha=sha)

    # -- deterministic phase ---------------------------------------------------------------

    async def deterministic(
        self, run: AgentRun, scope: CodeScope
    ) -> tuple[CodeAnalysis | None, list[str]]:
        window = run.task.context.time_range
        incident_start, since, until = window.start, window.start - scope.lookback, window.end
        notes: list[str] = []
        risks: list[CommitRisk] = []
        releases: dict[str, list[Release]] = {}
        tags_by_repo: dict[str, set[str]] = {}
        release_evidence: list[Evidence] = []
        commit_evidence: list[tuple[Evidence, list[str]]] = []

        for repo, targets in scope.repos().items():
            outcome, evidence = await run.call_tool(
                "list_releases",
                {"repo": repo, "limit": scope.max_commits},
                summary=f"Release tags in {repo}",
            )
            raw_releases = (outcome.data or {}).get("releases", []) if evidence else []
            if evidence is None:
                notes.append(f"Release tags unavailable for {repo}: {outcome.tool_call.error}")
            else:
                release_evidence.append(evidence)
            tags_by_repo[repo] = {str(r.get("tag")) for r in raw_releases}
            releases.update(
                parse_releases(raw_releases, scope.tag_template, [t.service for t in targets])
            )

            paths = [p for t in targets for p in t.paths]
            outcome, evidence = await run.call_tool(
                "search_commits",
                {
                    "repo": repo,
                    "since": iso(since),
                    "until": iso(until),
                    "paths": paths,
                    "limit": scope.max_commits,
                },
                summary=f"Commits touching {', '.join(paths)}",
            )
            if evidence is None:
                return None, [*notes, f"Commit search failed for {repo}: {outcome.tool_call.error}"]
            commit_evidence.append((evidence, paths))
            for raw in (outcome.data or {}).get("commits", []):
                files = [str(f.get("path", "")) for f in raw.get("files", [])]
                owner = next((t for t in targets if any(t.owns(f) for f in files)), targets[0])
                commit = Commit.from_tool(raw, owner.service, owner.dependency)
                risks.append(
                    CommitRisk(
                        commit=commit,
                        proximity=proximity(commit.date, incident_start, scope.lookback),
                        scope_weight=DEPENDENCY_WEIGHT if owner.dependency else 1.0,
                        categories=sorted({classify_path(f) for f in files if owner.owns(f)}),
                    )
                )

        await self._inspect(run, scope, risks, tags_by_repo)

        analysis = CodeAnalysis(
            service=scope.service,
            since=since,
            until=until,
            incident_start=incident_start,
            paths={t.service: list(t.paths) for t in scope.targets},
            risks=risks,
            releases=releases,
            threshold=scope.threshold,
        )
        analysis.compute_signals()

        # Evidence summaries now that the numbers are known.
        for evidence in release_evidence:
            latest = [f"{rs[0].tag} ({iso(rs[0].date)})" for rs in analysis.releases.values() if rs]
            evidence.summary = "Latest release tags: " + (", ".join(latest) or "none")
            notes.append(f"[{evidence.id}] releases")
        for evidence, paths in commit_evidence:
            evidence.summary = (
                f"{len(risks)} commits touching {', '.join(paths)} between {iso(since)} and "
                f"{iso(until)}; {len(analysis.suspects)} suspect(s)"
            )
            notes.append(f"[{evidence.id}] commits")
        return analysis, notes

    async def _inspect(
        self,
        run: AgentRun,
        scope: CodeScope,
        risks: list[CommitRisk],
        tags_by_repo: dict[str, set[str]],
    ) -> None:
        """Fetch diffs of the highest pre-scored commits and apply the risk rules."""
        rules = DiffRules(scope.risky_keys)
        targets = {t.service: t for t in scope.targets}
        candidates = sorted(
            (r for r in risks if r.pre_score > 0),
            key=lambda r: (-r.pre_score, -r.commit.date.timestamp()),
        )[: scope.max_diffs]
        for risk in candidates:
            target = targets[risk.commit.service]
            outcome, evidence = await run.call_tool(
                "get_diff",
                {"repo": target.repo, "sha": risk.commit.sha, "paths": list(target.paths)},
                summary=f"Diff of {risk.commit.short}",
            )
            if evidence is None:
                continue
            risk.facts = rules.facts(
                (outcome.data or {}).get("files", []),
                service=risk.commit.service,
                release_tags=tags_by_repo.get(target.repo, set()),
                template=scope.tag_template,
            )
            risk.inspected = True
            risk.evidence_id = evidence.id
            relevant = risk.risky_facts or risk.facts[:1]
            evidence.summary = (
                f"{risk.headline()} [{risk.commit.service}]: {risk.fact_summary()} "
                f"(risk {risk.score:.2f})"
            )
            evidence.timestamp = risk.commit.date
            evidence.link = self.commit_link(scope, target.repo, risk.commit.sha)
            evidence.data = {
                "commit": {
                    "sha": risk.commit.sha,
                    "subject": risk.commit.subject,
                    "author": risk.commit.author,
                    "date": iso(risk.commit.date),
                    "service": risk.commit.service,
                    "tags": risk.commit.tags,
                },
                "risk": risk.score,
                "facts": [f.description for f in risk.facts],
                "hunks": [{"file": f.file, "hunk": f.hunk} for f in relevant if f.hunk],
            }

    # -- investigation -------------------------------------------------------------------

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        variables = super().prompt_variables(run)
        scope = self.scope(run)
        window = run.task.context.time_range
        deps = [t for t in scope.targets if t.dependency]
        variables.update(
            repo=scope.primary.repo,
            paths=", ".join(scope.primary.paths),
            dependencies=", ".join(f"{t.service} ({', '.join(t.paths)})" for t in deps) or "none",
            start=iso(window.start),
            end=iso(window.end),
            lookback_start=iso(window.start - scope.lookback),
            tag_template=scope.tag_template,
            signals=", ".join(f"`{s}`" for s in SIGNALS),
        )
        return variables

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        try:
            scope = self.scope(run)
        except (LookupError, ConfigError) as exc:
            return run.failed(str(exc))
        analysis, notes = await self.deterministic(run, scope)
        if analysis is None:
            return run.failed("Could not read the repository: " + " ".join(notes))

        system, user = self.build_prompt(run)
        overview = "\n".join([*analysis.lines(), "Evidence ids: " + "; ".join(notes)])
        user = f"{user}\n\n## Overview (computed for you, deterministic)\n{overview}"
        result = await self.llm_loop(run, tool_specs, system, user)
        return self.finalize(result, analysis)

    def finalize(self, result: AgentResult, analysis: CodeAnalysis) -> AgentResult:
        """Data signals and status are authoritative: no invented suspects, none hidden."""
        update: dict[str, Any] = {"signals": list(analysis.signals)}
        if result.status in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL):
            update["status"] = (
                AgentStatus.SUCCESS if analysis.has_findings else AgentStatus.NO_SIGNAL
            )
        return result.model_copy(update=update)


AGENTS.register(CodeAgent)
