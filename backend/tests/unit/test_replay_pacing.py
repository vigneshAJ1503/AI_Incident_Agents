"""PR-039: replays started from the Web UI can unfold at a watchable pace."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from aiops.api.runner import REPLAY_DELAY_ENV, replay_tool_delay_s
from aiops.mcp.fixtures import ReplayMCPClient


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 0.0), ("", 0.0), ("0.5", 0.5), ("-3", 0.0), ("99", 10.0), ("fast", 0.0)],
)
def test_delay_from_env(raw: str | None, expected: float, monkeypatch: pytest.MonkeyPatch) -> None:
    if raw is None:
        monkeypatch.delenv(REPLAY_DELAY_ENV, raising=False)
    else:
        monkeypatch.setenv(REPLAY_DELAY_ENV, raw)
    assert replay_tool_delay_s() == expected


def test_replay_client_waits_before_answering(tmp_path: Path) -> None:
    fixture = tmp_path / "logs.json"
    fixture.write_text(
        json.dumps(
            {
                "tools": [],
                "calls": [
                    {
                        "tool": "search",
                        "arguments": {"q": "x"},
                        "result": {"text": "ok", "is_error": False},
                    }
                ],
            }
        )
    )

    async def call(delay: float) -> float:
        client = ReplayMCPClient("logs", fixture, delay_s=delay)
        start = time.perf_counter()
        await client.call_tool("search", {"q": "x"})
        return time.perf_counter() - start

    assert asyncio.run(call(0.0)) < 0.05
    assert asyncio.run(call(0.1)) >= 0.09
