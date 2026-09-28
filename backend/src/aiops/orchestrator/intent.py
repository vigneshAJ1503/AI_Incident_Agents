"""Intent of a question typed into the chat box (PR-041): an incident, or a platform question.

Incident questions ("Payment API is returning HTTP 500") start an investigation. Platform
questions ("what agents are running now?", "is the LLM configured?") are answered directly
from live platform data (``aiops.api.ask``) and never start an investigation.

Deterministic rules first, the ``fast`` LLM role only when the rules are unsure:

1. ``help``: "help", "what should I ask?", "examples"; ``agents_running`` ("what's
   running?", "is the payments agent running?"; with a service it needs an agent noun);
   ``recent_investigations`` ("recent failed incidents on order-service").
2. **Incident wins** when the question names a catalog service AND a symptom
   ("agents are failing on payment-service", "is payment-service down?").
3. Platform patterns (``agents_catalog``, ``health``). Each needs a platform noun (agents,
   LLM, a capability or its provider...) or a service-less phrasing ("what's running?",
   "what's down?").
4. A service or a symptom alone -> incident.
5. Otherwise unsure: the LLM (structured ``{intent, service, confidence}``) when one is
   configured, else incident (the safe default: the investigation asks to clarify).

The intent registry (``INTENTS``) is extensible: add a name + patterns here and an answer
builder in ``aiops.api.ask``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

from aiops.core.catalog import ServiceCatalog
from aiops.llm.base import ChatMessage, LLMError, LLMProvider
from aiops.llm.structured import generate_structured
from aiops.orchestrator.planner import find_services, find_symptoms, pick_service

PlatformIntent = Literal[
    "agents_running", "agents_catalog", "health", "recent_investigations", "help"
]
Intent = Literal[
    "agents_running", "agents_catalog", "health", "recent_investigations", "help", "incident"
]
Kind = Literal["platform", "incident"]
Source = Literal["rules", "llm", "default"]

#: Platform intents and what they answer (the LLM sees these descriptions).
INTENTS: dict[str, str] = {
    "agents_running": "which investigations/agents are running right now and what they do",
    "agents_catalog": "which agents exist, what they can check, their success rate",
    "health": "platform health: LLM configured, capabilities/data sources up or down",
    "recent_investigations": "past investigations/incidents, their root cause and outcome",
    "help": "what the user can ask, example questions",
}
#: Below this LLM confidence a platform guess falls back to an incident.
LLM_MIN_CONFIDENCE = 0.6

_F = re.IGNORECASE

_HELP = [
    re.compile(r"^\s*(help|\?+|examples?|usage|how does this work|what is this)\s*[?.!]*\s*$", _F),
    re.compile(r"\bwhat (should|can|could) i (ask|type|say)\b", _F),
    re.compile(r"\b(give me|show me|any) (some )?(example|sample) (questions?|prompts?)\b", _F),
    re.compile(r"\bhow (do|can|should) i use (this|you|the (app|tool|platform))\b", _F),
]
_HISTORY_NOUN = r"(incidents?|investigations?|outages?|rcas?|root causes?|postmortems?)"
_RECENT = [
    re.compile(
        rf"\b(recent|last|latest|previous|past|prior|earlier|older)\s+([\w-]+\s+){{0,3}}?{_HISTORY_NOUN}\b",
        _F,
    ),
    re.compile(
        rf"\b(show|list|find|search|get)\s+(me\s+)?(the\s+|all\s+)?([\w-]+\s+){{0,3}}?{_HISTORY_NOUN}\b",
        _F,
    ),
    re.compile(
        r"\bwhat happened (with|to|in|during) (the )?(last|latest|recent|previous|yesterday)", _F
    ),
    re.compile(rf"\b{_HISTORY_NOUN}\s+(history|so far|this week|today|yesterday)\b", _F),
    re.compile(rf"\bhow many\s+([\w-]+\s+){{0,2}}?{_HISTORY_NOUN}\b", _F),
]
_RUNNING_WORDS = r"(running|in progress|in[- ]flight|underway|active|busy|working)"
_AGENTS = r"(agents?|investigations?|runs?|jobs?)"
_RUNNING = [
    # "what's running?", "what is currently running", "what's in progress"
    re.compile(
        r"\bwhat('?s|\s+is|\s+are)?\s+(currently\s+|still\s+|now\s+)?"
        r"(running|in progress|in[- ]flight|underway)\b",
        _F,
    ),
    # "what agents are running now?", "which investigations are active", "any agents busy?"
    re.compile(
        rf"\b(what|which|any|are there( any)?|show|list)\s+([\w-]+\s+){{0,2}}?{_AGENTS}\s+"
        rf"(that\s+|which\s+)?(are\s+|is\s+)?(still\s+|currently\s+|now\s+)?{_RUNNING_WORDS}",
        _F,
    ),
    # "is the payments agent running?"
    re.compile(
        rf"\b(is|are)\s+(the\s+|any\s+|my\s+)?([\w-]+\s+){{0,2}}?{_AGENTS}\s+"
        rf"(still\s+|currently\s+)?{_RUNNING_WORDS}\b",
        _F,
    ),
    # "running agents", "active investigations", "status of the agents"
    re.compile(rf"\b(running|active|ongoing|current|in[- ]progress)\s+{_AGENTS}\b", _F),
    re.compile(rf"\b(status|progress) of (the |my )?(running |current )?{_AGENTS}\b", _F),
    re.compile(r"\b(anything|something) (running|in progress)\b", _F),
]
_CATALOG = [
    re.compile(r"\b(which|what)\s+(\w+\s+){0,2}?agents\b", _F),
    re.compile(r"\b(list|show|describe)\s+(me\s+)?(the\s+|all\s+|your\s+)?(\w+\s+)?agents\b", _F),
    re.compile(r"\bhow many agents\b", _F),
    re.compile(r"\bwhat (can|do) you (check|do|look at|investigate|inspect|monitor|cover)\b", _F),
    re.compile(r"\bwhat (are )?(your|the) (capabilities|tools|data sources|integrations)\b", _F),
    re.compile(r"\b(agent|agents) (catalog|registry|list)\b", _F),
    re.compile(r"\bwhich (data sources|tools|capabilities|integrations)\b", _F),
    re.compile(r"\bdo you have (an? |any )?\w*\s*agents?\b", _F),
]
_STATE_WORDS = (
    r"(up|down|configured|set ?up|reachable|unreachable|healthy|unhealthy|working|available|"
    r"connected|online|offline|ok|okay|enabled|disabled|broken|running)"
)
#: Platform components a health question may name (capability providers are added from
#: the profile at runtime, e.g. prometheus, elasticsearch, loki, jira).
PLATFORM_COMPONENTS = {
    "llm",
    "llms",
    "model",
    "models",
    "ai",
    "groq",
    "openai",
    "anthropic",
    "bedrock",
    "azure",
    "api",
    "backend",
    "store",
    "evidence store",
    "postgres",
    "postgresql",
    "mcp",
    "mcp servers",
    "capabilities",
    "data sources",
    "integrations",
    "platform",
    "system",
    "faults",
    "fault injection",
}
_HEALTH_GENERIC = [
    re.compile(
        r"^\s*what('?s|\s+is|\s+are)\s+(currently\s+)?(down|broken|unhealthy|offline|not working|unreachable)\b",
        _F,
    ),
    re.compile(r"\b(platform|system|service) (health|status)\b(?!.*\bof\b)", _F),
    re.compile(r"^\s*(health|health ?check|status)\s*[?.!]*\s*$", _F),
    re.compile(r"\b(is|are) (the )?(llm|model|ai)\b", _F),
    re.compile(r"\bwhich (llm|model|provider)\b", _F),
    re.compile(r"\b(llm|model) (provider|configured|config|key|status)\b", _F),
    re.compile(r"\bis (fault injection|faults?) (enabled|on|allowed)\b", _F),
    re.compile(r"\b(which|what) profile\b", _F),
    re.compile(r"\bare (all )?(the )?(capabilities|data sources|integrations|mcp servers)\b", _F),
]
_AGENT_NOUN = re.compile(r"\b(agents?|investigations?)\b", _F)
_HISTORY = re.compile(rf"\b{_HISTORY_NOUN}\b", _F)


class Classification(BaseModel):
    kind: Kind
    intent: Intent
    service: str | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    source: Source = "rules"
    reason: str = ""


class LLMIntent(BaseModel):
    """What the ``fast`` model may answer when the rules are unsure."""

    intent: Intent = Field(
        description="One of the platform intents, or 'incident' for a production problem."
    )
    service: str | None = Field(default=None, description="Catalog service named, or null.")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


@dataclass
class IntentClassifier:
    catalog: ServiceCatalog
    #: Extra platform component words for health questions (capability names + providers).
    components: frozenset[str] = frozenset()
    llm: LLMProvider | None = None

    @classmethod
    def for_profile(
        cls,
        catalog: ServiceCatalog,
        capabilities: Iterable[tuple[str, str]] = (),
        llm: LLMProvider | None = None,
    ) -> IntentClassifier:
        """``capabilities`` = ``(name, provider)`` pairs from the profile."""
        words: set[str] = set()
        for name, provider in capabilities:
            words.add(name.casefold())
            words.update(p for p in re.split(r"[_\-\s]+", provider.casefold()) if len(p) > 2)
            words.add(provider.casefold().replace("_", " "))
        return cls(catalog=catalog, components=frozenset(words), llm=llm)

    # -- rules -------------------------------------------------------------------------------

    def services(self, question: str) -> list[str]:
        return find_services(question, self.catalog)

    def _names_component(self, question: str) -> bool:
        lowered = question.casefold()
        for word in PLATFORM_COMPONENTS | self.components:
            if re.search(rf"\b{re.escape(word)}\b", lowered):
                return True
        return False

    def rules(self, question: str) -> Classification | None:
        """The rule-based answer, or ``None`` when unsure."""
        q = " ".join(question.split())
        names = self.services(q)
        service = pick_service(names, self.catalog) if names else None
        service = service or (names[0] if names else None)
        symptoms = [s for s in find_symptoms(q) if s != "deployment"]

        def platform(
            intent: PlatformIntent, reason: str, confidence: float = 0.9
        ) -> Classification:
            return Classification(
                kind="platform",
                intent=intent,
                service=service,
                confidence=confidence,
                reason=reason,
            )

        def incident(reason: str, confidence: float = 0.9) -> Classification:
            return Classification(
                kind="incident",
                intent="incident",
                service=service,
                confidence=confidence,
                reason=reason,
            )

        if any(p.search(q) for p in _HELP):
            return platform("help", "asks for help")
        # "what alerts are active on order-service?" is an incident; "is the payments agent
        # running?" is not: with a service, a running question needs an agent noun.
        if any(p.search(q) for p in _RUNNING) and (not service or _AGENT_NOUN.search(q)):
            return platform("agents_running", "asks what is running")
        recent = any(p.search(q) for p in _RECENT)
        if recent:
            return platform("recent_investigations", "asks about past investigations")
        if service and symptoms:
            return incident(f"names {service} and symptoms ({', '.join(symptoms)})")
        if any(p.search(q) for p in _CATALOG):
            return platform("agents_catalog", "asks which agents/capabilities exist")
        health_state = re.search(rf"\b(is|are|isn'?t|aren'?t)\b.*\b{_STATE_WORDS}\b", q, _F)
        if any(p.search(q) for p in _HEALTH_GENERIC) and not service:
            return platform("health", "asks about platform health")
        if health_state and self._names_component(q) and not service:
            return platform("health", "asks whether a platform component is up")
        if service:
            return incident(f"names {service}", 0.8)
        if symptoms:
            return incident(f"describes symptoms ({', '.join(symptoms)})", 0.7)
        if _HISTORY.search(q):
            return platform("recent_investigations", "mentions investigations", 0.6)
        return None

    # -- classification ------------------------------------------------------------------------

    async def classify(self, question: str) -> Classification:
        ruled = self.rules(question)
        if ruled is not None:
            return ruled
        if self.llm is not None:
            guessed = await self._llm(question)
            if guessed is not None:
                return guessed
        return Classification(
            kind="incident",
            intent="incident",
            confidence=0.5,
            source="default",
            reason="no platform question recognised",
        )

    async def _llm(self, question: str) -> Classification | None:
        assert self.llm is not None  # noqa: S101 (checked by the caller)
        intents = "\n".join(f"- {name}: {what}" for name, what in INTENTS.items())
        services = ", ".join(s.name for s in self.catalog.services)
        messages = [
            ChatMessage.system(
                "Classify a question typed into an incident-investigation assistant. "
                "'incident' = the user reports or asks about a production problem (errors, "
                "slowness, outages) to investigate. Platform intents are questions about the "
                f"assistant itself:\n{intents}\nServices: {services}. Answer with the intent, "
                "the service named (or null) and your confidence (0..1)."
            ),
            ChatMessage.user(question[:500]),
        ]
        try:
            answer, _ = await generate_structured(
                self.llm, messages, LLMIntent, role="fast", max_attempts=1
            )
        except LLMError:
            return None
        service = None
        if answer.service:
            resolved = self.catalog.resolve(answer.service).service
            service = resolved.name if resolved else None
        if answer.intent == "incident" or answer.confidence < LLM_MIN_CONFIDENCE:
            return Classification(
                kind="incident",
                intent="incident",
                service=service,
                confidence=answer.confidence if answer.intent == "incident" else 0.5,
                source="llm",
                reason="LLM: incident" if answer.intent == "incident" else "LLM unsure",
            )
        return Classification(
            kind="platform",
            intent=answer.intent,
            service=service,
            confidence=answer.confidence,
            source="llm",
            reason=f"LLM: {answer.intent}",
        )
