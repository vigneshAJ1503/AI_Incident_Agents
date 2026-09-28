"""Optional LLM-judge scorer for root causes (PR-040).

The rule-based scorer (``investigation.root_cause_keywords``) is the gate; the judge is a
second opinion that tolerates paraphrases. It needs a hosted LLM (``llm.provider:
openai_compat`` + key); without one it is skipped with a note, never faked.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from aiops.core.config import Settings
from aiops.core.models import TokenUsage
from aiops.core.prompts import Prompt, PromptLoader
from aiops.llm.base import ChatMessage, LLMError, LLMProvider
from aiops.llm.structured import generate_structured

PROMPT = "judge"


class JudgeVerdict(BaseModel):
    correct: bool = Field(description="Same underlying cause as the ground truth.")
    score: float = Field(ge=0, le=1, description="0 = unrelated, 1 = same cause.")
    reason: str = Field(default="", description="One sentence.")


def judge_unavailable(settings: Settings) -> str | None:
    """Why the judge can't run (None = it can)."""
    from aiops.llm.factory import config_problems

    llm = settings.llm
    problems = ["llm.provider is 'fake'"] if llm.provider == "fake" else config_problems(llm)
    if problems:
        return (
            "LLM judge skipped: no hosted LLM key configured ("
            + "; ".join(problems)
            + "; docs/setup/llm-providers.md). Root-cause accuracy is rule-based only."
        )
    return None


def judge_prompt(settings: Settings) -> Prompt:
    loader = PromptLoader(
        settings.config_dir / "prompts", overrides=settings.prompt_override_dirs()
    )
    return loader.load(PROMPT)


async def judge_root_cause(
    llm: LLMProvider, prompt: Prompt, ground_truth: str | None, reported: str | None
) -> tuple[JudgeVerdict | None, TokenUsage, str | None]:
    """(verdict, usage, error). A failing judge never fails the evaluation."""
    text = prompt.render(
        ground_truth=(ground_truth or "No incident: there is no root cause.").strip(),
        reported=(reported or "No root cause identified.").strip(),
    )
    try:
        verdict, usage = await generate_structured(
            llm,
            [ChatMessage.user(text)],
            JudgeVerdict,
            role="fast",
            description="Submit the grade.",
        )
    except LLMError as exc:
        return None, TokenUsage(), f"{type(exc).__name__}: {exc}"
    return verdict, usage, None
