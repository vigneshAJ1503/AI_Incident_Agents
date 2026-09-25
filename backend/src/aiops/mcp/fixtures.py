"""Record real MCP responses once, replay them forever (deterministic, zero-cost tests).

Fixture file (per capability): {"tools": [...], "calls": [{"tool", "arguments", "result"}]}
Replay matches calls by tool name + canonical JSON of the arguments.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiops.mcp.client import MCPClient, MCPClientError, MCPTool, MCPToolResult


def _key(tool: str, arguments: dict[str, Any]) -> str:
    return f"{tool}:{json.dumps(arguments, sort_keys=True, default=str)}"


class RecordingMCPClient(MCPClient):
    """Delegates to a live client and writes every exchange to ``path`` on close."""

    def __init__(self, inner: MCPClient, path: Path) -> None:
        super().__init__(inner.name, "recording://")
        self._inner = inner
        self._path = path
        self._recorded_tools: list[MCPTool] = []
        self._calls: list[dict[str, Any]] = []

    async def connect(self) -> None:
        await self._inner.connect()

    async def close(self) -> None:
        await self._inner.close()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "tools": [t.model_dump() for t in self._recorded_tools],
            "calls": self._calls,
        }
        self._path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")

    async def list_tools(self) -> list[MCPTool]:
        self._recorded_tools = await self._inner.list_tools()
        return self._recorded_tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout_s: float | None = None
    ) -> MCPToolResult:
        result = await self._inner.call_tool(name, arguments, timeout_s)
        self._calls.append({"tool": name, "arguments": arguments, "result": result.model_dump()})
        return result


class ReplayMCPClient(MCPClient):
    """Serves tools/calls from a fixture file; unknown calls are an error (no silent live calls)."""

    def __init__(self, name: str, path: Path) -> None:
        super().__init__(name, "replay://")
        if not path.is_file():
            raise MCPClientError(f"Fixture not found: {path}")
        payload = json.loads(path.read_text())
        self._fixture_tools = [MCPTool.model_validate(t) for t in payload.get("tools", [])]
        self._responses: dict[str, MCPToolResult] = {
            _key(c["tool"], c["arguments"]): MCPToolResult.model_validate(c["result"])
            for c in payload.get("calls", [])
        }
        self.path = path

    async def connect(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def list_tools(self) -> list[MCPTool]:
        return self._fixture_tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout_s: float | None = None
    ) -> MCPToolResult:
        try:
            return self._responses[_key(name, arguments)]
        except KeyError:
            raise MCPClientError(
                f"No recorded response for {name}({json.dumps(arguments, sort_keys=True)}) "
                f"in {self.path}. Re-record with --record."
            ) from None
