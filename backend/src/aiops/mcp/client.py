"""Thin wrapper around the MCP SDK client: lifecycle, connect retries, timeouts, errors.

The agent never cares whether the server runs in-process, in Docker or remotely.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp_types import TextContent
from pydantic import BaseModel, ConfigDict, Field

from aiops.core.config import MCPServerConfig

log = logging.getLogger(__name__)

ServerTarget = str | StdioServerParameters | MCPServer


class MCPClientError(Exception):
    """Connection/transport failure (not a tool-level error)."""


class MCPTool(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPToolResult(BaseModel):
    is_error: bool
    text: str
    structured: dict[str, Any] | None = None


def _root_cause(exc: BaseException) -> str:
    """Unwrap anyio ExceptionGroups to the first leaf error for a readable message."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__


def target_from_config(config: MCPServerConfig) -> ServerTarget:
    # Presence of url/command is enforced by MCPServerConfig validation.
    if config.transport == "http" and config.url:
        return config.url
    if config.transport == "stdio" and config.command:
        return StdioServerParameters(
            command=config.command, args=config.args, env=config.env or None
        )
    raise MCPClientError(f"Invalid MCP server config: {config}")


class MCPClient:
    def __init__(
        self,
        name: str,
        target: ServerTarget,
        *,
        timeout_s: float = 30.0,
        connect_attempts: int = 3,
        connect_backoff_s: float = 0.5,
    ) -> None:
        self.name = name
        self._target = target
        self._timeout_s = timeout_s
        self._connect_attempts = connect_attempts
        self._connect_backoff_s = connect_backoff_s
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._tools: list[MCPTool] | None = None

    @classmethod
    def from_config(cls, name: str, config: MCPServerConfig) -> MCPClient:
        return cls(name, target_from_config(config), timeout_s=config.timeout_s)

    async def __aenter__(self) -> MCPClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        last_error: BaseException | None = None
        for attempt in range(1, self._connect_attempts + 1):
            stack = AsyncExitStack()
            try:
                client = Client(self._target, read_timeout_seconds=self._timeout_s)
                # asyncio.timeout (not wait_for) keeps anyio cancel scopes in the same task.
                async with asyncio.timeout(self._timeout_s):
                    await stack.enter_async_context(client)
            except Exception as exc:  # transport errors vary by transport
                await stack.aclose()
                last_error = exc
                log.warning(
                    "MCP '%s' connect attempt %d failed: %s", self.name, attempt, _root_cause(exc)
                )
                if attempt < self._connect_attempts:
                    await asyncio.sleep(self._connect_backoff_s * 2 ** (attempt - 1))
                continue
            self._stack, self._client = stack, client
            return
        cause = _root_cause(last_error) if last_error else "unknown error"
        raise MCPClientError(
            f"Cannot connect to MCP server '{self.name}' ({self._describe()}): {cause}"
        ) from last_error

    async def close(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack, self._client, self._tools = None, None, None

    def _describe(self) -> str:
        target = self._target
        if isinstance(target, str):
            return target
        if isinstance(target, StdioServerParameters):
            return f"stdio:{target.command}"
        return "in-process"

    def _require(self) -> Client:
        if self._client is None:
            raise MCPClientError(f"MCP client '{self.name}' is not connected")
        return self._client

    async def list_tools(self) -> list[MCPTool]:
        if self._tools is None:
            async with asyncio.timeout(self._timeout_s):
                result = await self._require().list_tools()
            self._tools = [
                MCPTool(
                    name=t.name,
                    description=t.description or "",
                    input_schema=dict(t.input_schema or {}),
                )
                for t in result.tools
            ]
        return self._tools

    async def call_tool(
        self, name: str, arguments: dict[str, Any], timeout_s: float | None = None
    ) -> MCPToolResult:
        """Call a tool. Raises TimeoutError on timeout, MCPClientError on transport failure."""
        timeout = timeout_s or self._timeout_s
        try:
            async with asyncio.timeout(timeout):
                result = await self._require().call_tool(
                    name, arguments, read_timeout_seconds=timeout
                )
        except TimeoutError:
            raise
        except MCPClientError:
            raise
        except Exception as exc:
            raise MCPClientError(
                f"MCP '{self.name}' call '{name}' failed: {_root_cause(exc)}"
            ) from exc
        texts = [block.text for block in result.content if isinstance(block, TextContent)]
        structured = (
            result.structured_content if isinstance(result.structured_content, dict) else None
        )
        # Servers wrap non-object return values as {"result": value}; unwrap them.
        if structured is not None and set(structured) == {"result"}:
            inner = structured["result"]
            structured = inner if isinstance(inner, dict) else None
        return MCPToolResult(
            is_error=bool(result.is_error), text="\n".join(texts), structured=structured
        )
