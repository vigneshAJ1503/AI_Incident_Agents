"""Knowledge Agent (UC-09): symptoms -> runbook sections with citations.

Investigation loop:
  1. build queries from task hints (signals, log patterns, alert names), the
     context's symptoms and the question (fillers and service names removed);
  2. search the ``knowledge`` capability: service-filtered first, then globally,
     for the symptom query and the question query (at most 4 searches);
  3. fuse the results (service catalog runbooks get a small boost) and fetch the
     top relevant runbooks plus the service's own doc (owners, escalation);
  4. extract Diagnosis / Mitigation / Rollback / Escalation sections as evidence,
     each with a citation link (doc path + heading anchor);
  5. the LLM summarizes; deterministic signals, status and citation findings are
     merged in ``finalize`` (they can't be dropped or invented).

Vendor-neutral: search backend, allowlist and link template come from the
``knowledge`` capability; runbook names from the service catalog.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiops.agents.base import AgentRun, AgentSpec, BaseAgent, ToolLimitExceededError
from aiops.agents.knowledge_agent.analysis import (
    CITED_KINDS,
    MATCH_KINDS,
    SIGNAL_TERMS,
    SIGNALS,
    DocMatch,
    ExtractedSection,
    KnowledgeAnalysis,
    QueryPlan,
    RunbookFinding,
    Search,
    build_queries,
    derive_signals,
    extract_sections,
    extract_sections_by_heading,
    fuse,
    is_relevant,
    pattern_matches,
    section_text,
    service_words,
)
from aiops.agents.registry import AGENTS
from aiops.core.models import AgentResult, AgentStatus, ClaimKind, Evidence, EvidenceKind, Finding
from aiops.llm.base import ToolSpec

DEFAULT_SEARCH_K = 8
QUESTION_WEIGHT = 0.25  # question-query weight when symptoms are available
GLOBAL_WEIGHT = 0.8  # global search vs service-filtered search
GENERIC_BONUS = 0.5  # extra weight (x service weight) for docs that apply to every service
DEFAULT_TOP_DOCS = 3
SECTION_CHARS = 700
SERVICE_DOC_SECTIONS = ("ownership", "dependencies", "dashboards", "known failure modes")


@dataclass(frozen=True)
class KnowledgeScope:
    link_template: str | None
    search_k: int
    top_docs: int
    signal_terms: dict[str, str]


class KnowledgeAgent(BaseAgent):
    spec = AgentSpec(
        name="knowledge",
        version="1",
        description="Finds the runbook sections (diagnosis, mitigation, rollback, escalation) that match the symptoms, with citations.",
        capabilities=["knowledge"],
        evidence_kind=EvidenceKind.DOC,
        prompt="knowledge",
    )

    # -- scope ---------------------------------------------------------------------------

    def scope(self) -> KnowledgeScope:
        settings = self.deps.settings.capability("knowledge").settings
        return KnowledgeScope(
            link_template=settings.get("ui_link_template"),
            search_k=int(settings.get("search_k", DEFAULT_SEARCH_K)),
            top_docs=int(settings.get("top_docs", DEFAULT_TOP_DOCS)),
            signal_terms={**SIGNAL_TERMS, **settings.get("signal_terms", {})},
        )

    def link(self, scope: KnowledgeScope, path: str, anchor: str = "") -> str | None:
        if not scope.link_template:
            return None
        url = scope.link_template.format(path=path)
        return f"{url}#{anchor}" if anchor else url

    def query_plan(self, run: AgentRun, scope: KnowledgeScope) -> QueryPlan:
        ctx = run.task.context
        names = [n for s in self.deps.catalog.services for n in (s.name, *s.aliases)]
        return build_queries(
            ctx.question,
            ctx.symptoms,
            run.task.hints,
            service_words=service_words(names),
            signal_terms=scope.signal_terms,
        )

    # -- deterministic phase ---------------------------------------------------------------

    async def _call(
        self, run: AgentRun, tool: str, args: dict[str, Any], summary: str
    ) -> tuple[Any, Evidence | None]:
        try:
            outcome, evidence = await run.call_tool(tool, args, summary=summary)
        except ToolLimitExceededError:
            return None, None
        return outcome.data, evidence

    async def deterministic(
        self, run: AgentRun, scope: KnowledgeScope
    ) -> tuple[KnowledgeAnalysis | None, list[str]]:
        ctx = run.task.context
        plan = self.query_plan(run, scope)
        analysis = KnowledgeAnalysis(plan=plan)
        notes: list[str] = []
        service = self.deps.catalog.get(ctx.service) if ctx.service else None
        catalog_docs = set(service.runbooks) if service else set()

        index, index_ev = await self._call(run, "list_docs", {}, "Knowledge base index")
        documents = (index or {}).get("documents", []) if isinstance(index, dict) else []
        if index_ev is not None:
            index_ev.summary = f"Knowledge base index: {len(documents)} documents"
            notes.append(f"[{index_ev.id}] index")

        # The question is a weak source next to structured symptoms: it only breaks ties then.
        question_weight = QUESTION_WEIGHT if plan.symptom_query else 1.0
        searches: list[Search] = []
        for source, query, weight in (
            ("symptoms", plan.symptom_query, 1.0),
            ("question", plan.question_query, question_weight),
        ):
            if not query:
                continue
            if service:
                searches.append(Search(f"{source}/service", query, [service.name], weight, source))
            # Generic runbooks (no services) only show up here; give them half a service match.
            bonus = weight * GENERIC_BONUS if service else 0.0
            searches.append(
                Search(f"{source}/all", query, [], weight * GLOBAL_WEIGHT, source, bonus)
            )

        results: list[tuple[Search, dict[str, Any]]] = []
        failures = 0
        for search in searches:
            args: dict[str, Any] = {"query": search.query, "k": scope.search_k, "max_per_doc": 2}
            if search.services:
                args["services"] = search.services
            data, evidence = await self._call(run, "search", args, f"Search ({search.label})")
            if evidence is None or not isinstance(data, dict):
                failures += 1
                continue
            hits = data.get("results") or []
            top = hits[0] if hits else None
            evidence.summary = f"Search ({search.label}): {len(hits)} sections" + (
                f"; best '{top['heading_path']}' (score {top['score']})" if top else ""
            )
            evidence.link = self.link(scope, top["path"], top.get("anchor", "")) if top else None
            notes.append(f"[{evidence.id}] search_{search.label.replace('/', '_')}")
            results.append((search, data))
        if searches and failures == len(searches):
            return None, ["Every knowledge search failed."]

        analysis.ranked = fuse(results, catalog_docs)
        require_symptoms = bool(plan.symptom_query)
        relevant = [
            d
            for d in analysis.ranked
            if d.doc_type == "runbook" and is_relevant(d, require_symptoms=require_symptoms)
        ]
        for doc in relevant[: scope.top_docs]:
            finding = await self._fetch_runbook(run, scope, doc, plan)
            if finding is not None:
                analysis.runbooks.append(finding)

        if service:
            await self._fetch_service_doc(run, scope, service.name, documents, analysis)
        analysis.signals = derive_signals(analysis)
        return analysis, notes

    async def _fetch_runbook(
        self, run: AgentRun, scope: KnowledgeScope, doc: DocMatch, plan: QueryPlan
    ) -> RunbookFinding | None:
        data, evidence = await self._call(run, "get_doc", {"path": doc.path}, f"Runbook {doc.path}")
        if evidence is None or not isinstance(data, dict):
            return None
        sections = extract_sections(str(data.get("content", "")))
        matchable = [s for kind in MATCH_KINDS for s in sections.get(kind, [])]
        known = pattern_matches(plan.patterns, matchable)
        finding = RunbookFinding(
            doc=doc,
            evidence_id=evidence.id,
            link=self.link(scope, doc.path, doc.best_anchor),
            sections=sections,
            known_issues=known,
        )
        cited = {kind: sections[kind] for kind in CITED_KINDS if sections.get(kind)}
        evidence.summary = (
            f"Runbook '{doc.title}' ({doc.path}): best match '{doc.best_heading_path}' "
            f"({doc.matched}/{doc.terms} query words); sections: {', '.join(cited) or 'none'}"
        )
        evidence.link = finding.link
        evidence.data = {
            "path": doc.path,
            "title": doc.title,
            "doc_type": doc.doc_type,
            "best_section": doc.best_heading_path,
            "coverage": round(doc.coverage, 2),
            "snippet": doc.best_snippet,
            "citations": [
                {"section": s.heading_path, "link": self.link(scope, doc.path, s.anchor)}
                for kind in cited
                for s in cited[kind]
            ],
            "sections": {kind: section_text(secs, SECTION_CHARS) for kind, secs in cited.items()},
            "known_issue_matches": [
                {"pattern": p, "section": s.heading_path, "overlap": o} for p, s, o in known
            ],
        }
        return finding

    async def _fetch_service_doc(
        self,
        run: AgentRun,
        scope: KnowledgeScope,
        service: str,
        documents: list[dict[str, Any]],
        analysis: KnowledgeAnalysis,
    ) -> None:
        path = next(
            (
                d["path"]
                for d in documents
                if d.get("doc_type") == "service" and service in (d.get("services") or [])
            ),
            None,
        )
        if path is None:
            return
        data, evidence = await self._call(run, "get_doc", {"path": path}, f"Service doc {path}")
        if evidence is None or not isinstance(data, dict):
            return
        sections = [
            s
            for s in extract_sections_by_heading(str(data.get("content", "")))
            if s.heading_path.rsplit(" > ", 1)[-1].casefold() in SERVICE_DOC_SECTIONS
        ]
        evidence.summary = f"Service doc for {service} ({path}): owners, dependencies, dashboards"
        evidence.link = self.link(scope, path)
        evidence.data = {
            "path": path,
            "title": data.get("title"),
            "doc_type": "service",
            "sections": {
                s.heading_path.rsplit(" > ", 1)[-1]: section_text([s], SECTION_CHARS)
                for s in sections
            },
        }
        analysis.context_docs.append((path, evidence.id, evidence.link))

    # -- investigation -------------------------------------------------------------------

    def prompt_variables(self, run: AgentRun) -> dict[str, object]:
        variables = super().prompt_variables(run)
        variables["signals"] = ", ".join(f"`{s}`" for s in SIGNALS)
        return variables

    def overview(self, analysis: KnowledgeAnalysis, notes: list[str]) -> str:
        plan = analysis.plan
        lines = [
            f"Symptom query (from hints/symptoms): {plan.symptom_query or 'none'}",
            f"Question query: {plan.question_query or 'none'}",
        ]
        if plan.empty:
            lines.append(
                "No symptoms to search for: no hints, no symptoms and no symptom words in the question."
            )
        if analysis.runbooks:
            lines.append("Top runbooks (fused rank; relevant = enough query words in one section):")
            for i, rb in enumerate(analysis.runbooks, start=1):
                d = rb.doc
                lines.append(
                    f"  {i}. [{rb.evidence_id}] {d.path} '{d.title}': best section "
                    f"'{d.best_heading_path}' ({d.matched}/{d.terms} query words, {d.source} query)"
                )
            for rb in analysis.runbooks:
                for pattern, section, overlap in rb.known_issues:
                    lines.append(
                        f"Known issue documented: '{pattern}' is described in "
                        f"'{section.heading_path}' ({overlap:.0%} of its words) [{rb.evidence_id}]"
                    )
            lines.append("Extracted sections:")
            for rb in analysis.runbooks:
                lines.append(f"  [{rb.evidence_id}] {rb.doc.path}")
                for kind in CITED_KINDS:
                    secs: list[ExtractedSection] = rb.sections.get(kind, [])
                    if secs:
                        text = section_text(secs, 400).replace("\n", " ")
                        lines.append(f"    {kind.title()}: {text}")
        elif not plan.empty:
            lines.append("No runbook section matched the symptoms closely enough.")
        for path, evidence_id, _ in analysis.context_docs:
            lines.append(f"Service doc (owners, on-call, dependencies): [{evidence_id}] {path}")
        lines.append("Evidence ids: " + ("; ".join(notes) or "none"))
        lines.append(f"Deterministic signals: {', '.join(analysis.signals) or 'none'}")
        return "\n".join(lines)

    async def investigate(self, run: AgentRun, tool_specs: list[ToolSpec]) -> AgentResult:
        scope = self.scope()
        analysis, notes = await self.deterministic(run, scope)
        if analysis is None:
            return run.failed("Could not search the knowledge base: " + " ".join(notes))
        for rb in analysis.runbooks:
            notes.append(f"[{rb.evidence_id}] runbook")
        for _, evidence_id, _ in analysis.context_docs:
            notes.append(f"[{evidence_id}] service_doc")

        system, user = self.build_prompt(run)
        user = f"{user}\n\n## Overview (computed for you, deterministic)\n{self.overview(analysis, notes)}"
        result = await self.llm_loop(run, tool_specs, system, user)
        return self.finalize(result, analysis, run)

    def finalize(
        self, result: AgentResult, analysis: KnowledgeAnalysis, run: AgentRun
    ) -> AgentResult:
        """Signals and status are data-derived; citations are always attached as findings."""
        if result.status not in (AgentStatus.SUCCESS, AgentStatus.NO_SIGNAL):
            return result.model_copy(update={"signals": analysis.signals})
        status = AgentStatus.SUCCESS if analysis.runbooks else AgentStatus.NO_SIGNAL
        return result.model_copy(
            update={
                "status": status,
                "signals": analysis.signals,
                "findings": [*result.findings, *citation_findings(analysis, run)],
            }
        )


def citation_findings(analysis: KnowledgeAnalysis, run: AgentRun) -> list[Finding]:
    """One FACT per top runbook (rank, path, section, link), plus known issues and mitigations."""
    findings: list[Finding] = []
    for i, rb in enumerate(analysis.runbooks, start=1):
        d = rb.doc
        findings.append(
            Finding(
                kind=ClaimKind.FACT,
                type="runbook_match",
                description=(
                    f"Runbook #{i}: {d.path} ('{d.title}'), section '{d.best_heading_path}' "
                    f"matches {d.matched}/{d.terms} query words. {rb.link or ''}"
                ).strip(),
                evidence_ids=[rb.evidence_id],
            )
        )
        for pattern, section, _ in rb.known_issues:
            findings.append(
                Finding(
                    kind=ClaimKind.OBSERVATION,
                    type="known_issue",
                    description=f"Known issue documented: '{pattern}' is described in '{section.heading_path}'.",
                    evidence_ids=[rb.evidence_id],
                )
            )
        steps = rb.sections.get("mitigation") or rb.sections.get("rollback")
        if steps:
            findings.append(
                Finding(
                    kind=ClaimKind.RECOMMENDATION,
                    type="documented_mitigation",
                    description=f"Follow '{steps[0].heading_path}' in {d.path}.",
                    evidence_ids=[rb.evidence_id],
                )
            )
    if not analysis.runbooks and run.evidence:
        queries = " | ".join(
            q for q in (analysis.plan.symptom_query, analysis.plan.question_query) if q
        )
        findings.append(
            Finding(
                kind=ClaimKind.FACT,
                type="no_relevant_docs",
                description=(
                    f"No runbook section matched (queries: {queries})."
                    if queries
                    else "No symptoms were given, so no runbook was searched."
                ),
                evidence_ids=[run.evidence[0].id],
            )
        )
    return findings


AGENTS.register(KnowledgeAgent)
