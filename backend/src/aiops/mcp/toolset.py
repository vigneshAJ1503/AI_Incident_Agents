"""Allowlisted, guarded view of one capability's MCP tools, as seen by an agent.

Every call is: allowlist check -> timeout -> redaction -> truncation -> audit.
Output is wrapped in <tool_output> so prompts can treat it as data, never instructions.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict

from aiops.core.config import CapabilityConfig, GuardrailsConfig
from aiops.core.guardrails.audit import AuditRecord, AuditSink
from aiops.core.guardrails.redaction import Redactor
from aiops.core.models import ToolCall, new_id
from aiops.llm.base import ToolSpec
from aiops.mcp.client import MCPClient, MCPClientError

log = logging.getLogger(__name__)


class ToolOutcome(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    tool_call: ToolCall
    ok: bool
    content: str  # redacted, truncated, wrapped: what the LLM sees
    data: Any = None  # redacted parsed JSON (if the tool returned JSON), for deterministic code
    text: str = ""  # redacted full text


def wrap_tool_output(tool: str, body: str) -> str:
    safe = body.replace("</tool_output>", "&lt;/tool_output&gt;")
    return f'<tool_output tool="{tool}">\n{safe}\n</tool_output>'


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated {len(text) - limit} chars]"


class Toolset:
    def __init__(
        self,
        capability: str,
        config: CapabilityConfig,
        client: MCPClient,
        *,
        agent: str,
        guardrails: GuardrailsConfig,
        audit: AuditSink,
        investigation_id: str | None = None,
        allowlist: Iterable[str] | None = None,
    ) -> None:
        """``allowlist`` overrides ``config.tool_allowlist``; only the approval executor
        passes one (``config.write_allowlist``, see MCPRegistry.write_toolset)."""
        self.capability = capability
        self.config = config
        self._client = client
        self._agent = agent
        self._guardrails = guardrails
        self._audit = audit
        self._investigation_id = investigation_id
        self._allowed = set(config.tool_allowlist if allowlist is None else allowlist)

    async def specs(self) -> list[ToolSpec]:
        """Tool definitions for the LLM — only allowlisted tools the server actually has."""
        server_tools = await self._client.list_tools()
        by_name = {t.name: t for t in server_tools}
        missing = self._allowed - set(by_name)
        if missing:
            log.warning(
                "Capability '%s': allowlisted tools not on server: %s",
                self.capability,
                sorted(missing),
            )
        return [
            ToolSpec(
                name=t.name,
                description=t.description,
                parameters=t.input_schema or {"type": "object", "properties": {}},
            )
            for t in server_tools
            if t.name in self._allowed
        ]

    def is_allowed(self, tool: str) -> bool:
        return tool in self._allowed

    async def call(self, tool: str, arguments: dict[str, Any] | None = None) -> ToolOutcome:
        arguments = arguments or {}
        redactor = Redactor(self._guardrails.redact)
        request_id = new_id("req")
        call = ToolCall(
            agent=self._agent,
            capability=self.capability,
            tool=tool,
            arguments=redactor.data(arguments),
        )
        started = time.perf_counter()
        outcome = await self._execute(call, tool, arguments, redactor)
        outcome.tool_call.duration_ms = round((time.perf_counter() - started) * 1000, 2)
        self._audit.record(
            AuditRecord(
                request_id=request_id,
                investigation_id=self._investigation_id,
                tool_call=outcome.tool_call,
                redactions=dict(redactor.counts),
            )
        )
        return outcome

    async def _execute(
        self, call: ToolCall, tool: str, arguments: dict[str, Any], redactor: Redactor
    ) -> ToolOutcome:
        def failed(status: str, message: str) -> ToolOutcome:
            call.status = status  # type: ignore[assignment]
            call.error = message
            return ToolOutcome(
                tool_call=call, ok=False, content=wrap_tool_output(tool, f"ERROR: {message}")
            )

        if not self.is_allowed(tool):
            allowed = ", ".join(sorted(self._allowed))
            return failed(
                "blocked",
                f"tool '{tool}' is not allowed for capability '{self.capability}' (allowed: {allowed})",
            )
        try:
            result = await self._client.call_tool(
                tool, arguments, timeout_s=self.config.limits.query_timeout_s
            )
        except TimeoutError:
            return failed(
                "timeout", f"tool '{tool}' timed out after {self.config.limits.query_timeout_s}s"
            )
        except MCPClientError as exc:
            return failed("error", str(exc))

        data: Any = result.structured
        if data is None:
            try:
                data = json.loads(result.text)
            except (ValueError, TypeError):
                data = None
        if data is not None:
            data = redactor.data(data)
            text = json.dumps(data, ensure_ascii=False, default=str)
        else:
            text = redactor.text(result.text)

        call.result_chars = len(text)
        if result.is_error:
            call.status = "error"
            call.error = truncate(text, 500)
        content = wrap_tool_output(tool, truncate(text, self._guardrails.max_tool_output_chars))
        return ToolOutcome(
            tool_call=call, ok=not result.is_error, content=content, data=data, text=text
        )
