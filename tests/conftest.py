# Shared fixtures for lictor's test suite: a `paths` fixture that points
# every XDG-ish lictor directory at a throwaway tmp_path, and a FakeTransport
# that implements the SDK's Transport ABC so brain.py / app.py can be
# exercised against a scripted conversation without a real `claude` CLI
# subprocess.

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from claude_agent_sdk import Transport


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("LICTOR_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("LICTOR_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LICTOR_CONFIG_HOME", str(tmp_path / "config"))

    from lictor import config as config_mod

    resolved = config_mod.Paths.resolve()
    resolved.ensure()
    return resolved


class FakeTransport(Transport):
    """A scripted Transport: any `control_request` written to it (e.g. the
    SDK's "initialize" handshake) gets an immediate matching
    `control_response`; any `user`-type message (an actual prompt) gets a
    canned system/init (first time only), an assistant text reply, and a
    result -- enough for brain.submit() to run end to end with no
    subprocess and no network.
    """

    def __init__(self, options: Any = None, *, reply_text: str = "fake reply") -> None:
        self.options = options
        self.reply_text = reply_text
        self.session_id = "fake-session-0001"
        self.written: list[dict[str, Any]] = []
        self._out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._connected = False
        self._closed = False
        self._init_sent = False

    async def connect(self) -> None:
        self._connected = True

    async def write(self, data: str) -> None:
        message = json.loads(data)
        self.written.append(message)
        msg_type = message.get("type")
        if msg_type == "control_request":
            await self._reply_control(message)
        elif msg_type == "user":
            await self._reply_user(message)

    async def _reply_control(self, message: dict[str, Any]) -> None:
        request = message.get("request", {})
        response: dict[str, Any] = {"request_id": message["request_id"], "subtype": "success"}
        if request.get("subtype") == "initialize":
            response["commands"] = []
            response["output_style"] = "default"
        await self._out.put({"type": "control_response", "response": response})

    async def _reply_user(self, message: dict[str, Any]) -> None:
        if not self._init_sent:
            self._init_sent = True
            await self._out.put(
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": self.session_id,
                    "model": "claude-fake-1",
                    "tools": [],
                    "mcp_servers": [],
                }
            )

        prompt_text = message.get("message", {}).get("content", "")
        await self._out.put(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-fake-1",
                    "content": [{"type": "text", "text": f"{self.reply_text} ({prompt_text})"}],
                },
                "session_id": self.session_id,
                "parent_tool_use_id": None,
            }
        )
        await self._out.put(
            {
                "type": "result",
                "subtype": "success",
                "duration_ms": 1,
                "duration_api_ms": 1,
                "is_error": False,
                "num_turns": 1,
                "session_id": self.session_id,
                "total_cost_usd": 0.0001,
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "result": self.reply_text,
                "terminal_reason": "completed",
            }
        )

    async def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            message = await self._out.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self._closed = True
        await self._out.put(None)

    def is_ready(self) -> bool:
        return self._connected and not self._closed

    async def end_input(self) -> None:
        pass


@pytest.fixture
def transport_factory():
    def _factory(options: Any = None) -> FakeTransport:
        return FakeTransport(options)

    return _factory
