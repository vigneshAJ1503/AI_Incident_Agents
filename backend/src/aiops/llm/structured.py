"""Structured output that works on any tool-calling model.

Instead of provider-specific JSON modes, the model must call a ``submit`` tool whose
parameters are the Pydantic schema. Invalid output gets one corrective retry,
which small free-tier models often need.
"""

from __future__ import annotations

from pydantic import BaseModel, ValidationError

from aiops.core.config import ModelRole
from aiops.core.models import TokenUsage
from aiops.llm.base import ChatMessage, LLMError, LLMProvider, ToolSpec

SUBMIT_TOOL = "submit"


def submit_tool[T: BaseModel](schema: type[T], description: str) -> ToolSpec:
    return ToolSpec(
        name=SUBMIT_TOOL, description=description, parameters=schema.model_json_schema()
    )


async def generate_structured[T: BaseModel](
    provider: LLMProvider,
    messages: list[ChatMessage],
    schema: type[T],
    *,
    role: ModelRole = "fast",
    description: str = "Submit the final answer.",
    max_attempts: int = 2,
) -> tuple[T, TokenUsage]:
    tool = submit_tool(schema, description)
    history = list(messages)
    usage = TokenUsage()
    last_error = "no attempt"
    for _ in range(max_attempts):
        response = await provider.generate(history, tools=[tool], tool_choice="required", role=role)
        usage = usage + response.usage
        call = next((c for c in response.tool_calls if c.name == SUBMIT_TOOL), None)
        if call is None:
            last_error = f"the model did not call '{SUBMIT_TOOL}'"
        elif call.parse_error:
            last_error = f"arguments were not valid JSON: {call.parse_error}"
        else:
            try:
                return schema.model_validate(call.arguments), usage
            except ValidationError as exc:
                last_error = f"arguments did not match the schema: {exc.errors(include_url=False)}"
        history = [
            *history,
            response.as_message(),
            *(
                ChatMessage.tool_result(c.id, f"Rejected: {last_error}")
                for c in response.tool_calls
            ),
            ChatMessage.user(
                f"Your previous answer was rejected ({last_error}). Call '{SUBMIT_TOOL}' again with valid arguments."
            ),
        ]
    raise LLMError(f"Structured output failed after {max_attempts} attempts: {last_error}")
