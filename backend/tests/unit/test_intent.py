"""Question intent (PR-041): platform questions vs incidents, rules first, the fast LLM only
when the rules are unsure. Zero tokens: the LLM is a FakeLLMProvider."""

from __future__ import annotations

import pytest

from aiops.core.catalog import ServiceCatalog
from aiops.core.config import Settings, load_settings
from aiops.llm.fake import FakeLLMProvider, tool_call
from aiops.orchestrator.intent import IntentClassifier
from tests.conftest import REPO_ROOT


@pytest.fixture(scope="module")
def settings() -> Settings:
    return load_settings("local", REPO_ROOT / "config")


@pytest.fixture(scope="module")
def classifier(settings: Settings) -> IntentClassifier:
    caps = [(name, cap.provider) for name, cap in settings.capabilities.items()]
    return IntentClassifier.for_profile(ServiceCatalog.from_settings(settings), caps)


PHRASINGS: list[tuple[str, str]] = [
    # agents_running
    ("what are the agents that are running now?", "agents_running"),
    ("what agents are running now?", "agents_running"),
    ("What's running?", "agents_running"),
    ("what is currently running", "agents_running"),
    ("which investigations are in progress?", "agents_running"),
    ("any agents busy right now?", "agents_running"),
    ("is the payments agent running?", "agents_running"),
    ("show running investigations", "agents_running"),
    ("status of the agents", "agents_running"),
    # agents_catalog
    ("which agents do you have?", "agents_catalog"),
    ("What can you check?", "agents_catalog"),
    ("list all agents", "agents_catalog"),
    ("how many agents are there", "agents_catalog"),
    ("what data sources do you use? which capabilities?", "agents_catalog"),
    # health
    ("is prometheus up?", "health"),
    ("is the LLM configured?", "health"),
    ("what's down?", "health"),
    ("is elasticsearch reachable", "health"),
    ("which LLM provider is used?", "health"),
    ("is fault injection enabled?", "health"),
    ("platform health", "health"),
    # recent_investigations
    ("what happened with the last payment incident?", "recent_investigations"),
    ("recent incidents on order-service", "recent_investigations"),
    ("show me the failed investigations", "recent_investigations"),
    ("how many critical incidents this week", "recent_investigations"),
    ("previous RCAs for checkout", "recent_investigations"),
    # help
    ("help", "help"),
    ("what should I ask?", "help"),
    ("give me some example questions", "help"),
    # incidents (the tricky ones included)
    ("agents are failing on payment-service", "incident"),
    ("is payment-service down?", "incident"),
    ("Payment API is returning HTTP 500 in production", "incident"),
    ("Orders are failing intermittently in production", "incident"),
    ("Why are orders timing out in production?", "incident"),
    ("Login and checkout requests are failing in production", "incident"),
    ("Payments are slow in production", "incident"),
    ("what is going on with payment-service?", "incident"),
    ("what alerts are active on order-service?", "incident"),
    ("the database is down", "incident"),
    ("is the checkout working?", "incident"),
    ("Something is broken", "incident"),
    ("payment errors since the last deploy", "incident"),
]


@pytest.mark.parametrize(("question", "intent"), PHRASINGS)
async def test_rules_classify(classifier: IntentClassifier, question: str, intent: str) -> None:
    result = await classifier.classify(question)
    assert result.intent == intent, (question, result)
    assert result.kind == ("incident" if intent == "incident" else "platform")


def test_enough_phrasings() -> None:
    assert len(PHRASINGS) >= 25
    assert {i for _, i in PHRASINGS} == {
        "agents_running",
        "agents_catalog",
        "health",
        "recent_investigations",
        "help",
        "incident",
    }


def test_rules_resolve_the_service(classifier: IntentClassifier) -> None:
    recent = classifier.rules("what happened with the last payment incident?")
    assert recent is not None and recent.service == "payment-service"
    incident = classifier.rules("agents are failing on payment-service")
    assert incident is not None and incident.service == "payment-service"
    assert "symptoms" in incident.reason


def test_unsure_questions_default_to_incident(classifier: IntentClassifier) -> None:
    assert classifier.rules("hello world") is None
    assert classifier.rules("what's going on?") is None


async def test_llm_decides_when_rules_are_unsure(settings: Settings) -> None:
    catalog = ServiceCatalog.from_settings(settings)
    llm = FakeLLMProvider(
        [tool_call("submit", {"intent": "agents_catalog", "service": None, "confidence": 0.9})]
    )
    result = await IntentClassifier(catalog, llm=llm).classify("tell me about yourself")
    assert (result.kind, result.intent, result.source) == ("platform", "agents_catalog", "llm")
    assert llm.requests[0]["role"] == "fast"
    assert llm.requests[0]["tool_choice"] != "auto" or llm.requests[0]["tools"]


async def test_llm_low_confidence_or_service_means_incident(settings: Settings) -> None:
    catalog = ServiceCatalog.from_settings(settings)
    unsure = FakeLLMProvider(
        [tool_call("submit", {"intent": "health", "service": None, "confidence": 0.3})]
    )
    result = await IntentClassifier(catalog, llm=unsure).classify("hmm, thoughts?")
    assert (result.kind, result.intent, result.source) == ("incident", "incident", "llm")
    incident = FakeLLMProvider(
        [tool_call("submit", {"intent": "incident", "service": "payments", "confidence": 0.8})]
    )
    result = await IntentClassifier(catalog, llm=incident).classify("things feel off")
    assert (result.kind, result.service) == ("incident", "payment-service")


async def test_clear_questions_never_call_the_llm(settings: Settings) -> None:
    llm = FakeLLMProvider()  # empty script: any call would raise
    classifier = IntentClassifier(ServiceCatalog.from_settings(settings), llm=llm)
    assert (await classifier.classify("what agents are running now?")).intent == "agents_running"
    assert (await classifier.classify("is payment-service down?")).intent == "incident"
    assert llm.requests == []


async def test_llm_errors_fall_back_to_incident(settings: Settings) -> None:
    llm = FakeLLMProvider()  # script exhausted -> LLMError
    result = await IntentClassifier(ServiceCatalog.from_settings(settings), llm=llm).classify(
        "tell me something"
    )
    assert (result.kind, result.source) == ("incident", "default")
