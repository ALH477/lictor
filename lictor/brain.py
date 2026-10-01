"""Brain: owns the ClaudeSDKClient lifecycle and runs one turn at a time.

Everything in here runs on the daemon asyncio loop thread that app.py
starts; it is driven from the main thread only through
``asyncio.run_coroutine_threadsafe`` (see ``App.call`` / ``App.wait_turn``).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from . import options as options_mod

if TYPE_CHECKING:
    from .repl import Submission

#: Tool-result content longer than this is truncated in the record itself
#: and spilled in full to an artifact file.
ARTIFACT_INLINE_LIMIT = 2000


@dataclass
class TurnSummary:
    num_turns: int | None = None
    usage: dict[str, Any] | None = None
    total_cost_usd: float | None = None
    terminal_reason: str | None = None
    session_id: str | None = None
    is_error: bool = False
    subtype: str | None = None


class Brain:
    def __init__(
        self,
        cfg: Any,
        state: Any,
        records: Any,
        approvals: Any,
        renderer: Any,
        transport_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.state = state
        self.records = records
        self.approvals = approvals
        self.renderer = renderer
        self.transport_factory = transport_factory
        self.client: ClaudeSDKClient | None = None

    def _build_options(self, *, resume: str | None) -> Any:
        fresh_cid = None if resume else self.state.cid
        can_use_tool = self.approvals.can_use_tool if self.approvals is not None else None
        return options_mod.build_options(
            self.cfg,
            self.state,
            resume=resume,
            fresh_cid=fresh_cid,
            can_use_tool=can_use_tool,
        )

    async def start(self, resume: str | None = None) -> None:
        """Connect to Claude. Does not consume any messages -- the init
        ``SystemMessage`` sits buffered until the first ``submit()`` call's
        ``receive_response()`` reads it, which is where ``state.claude_session``
        gets set."""
        opts = self._build_options(resume=resume)
        transport = self.transport_factory(opts) if self.transport_factory is not None else None
        self.client = ClaudeSDKClient(opts, transport=transport)
        await self.client.connect()

    async def submit(self, sub: Submission) -> TurnSummary:
        if self.client is None:
            raise RuntimeError("Brain.start() must be awaited before submit()")

        self.records.append("prompt", {"text": sub.text, "to": sub.to, "source": sub.source})
        await self.client.query(sub.text)

        summary = TurnSummary()

        async for msg in self.client.receive_response():
            if isinstance(msg, StreamEvent):
                self.renderer.on_stream(msg)
                continue

            self.renderer.on_message(msg)

            if isinstance(msg, SystemMessage) and msg.subtype == "init":
                self.state.claude_session = msg.data.get("session_id")
                self.state.model = msg.data.get("model")

            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        self.records.append("assistant_text", {"text": block.text})
                    elif isinstance(block, ToolUseBlock):
                        self.records.append(
                            "tool_use", {"id": block.id, "name": block.name, "input": block.input}
                        )
                    elif isinstance(block, ThinkingBlock):
                        self.records.append("thinking", {"chars": len(block.thinking)})

            elif isinstance(msg, UserMessage):
                content = msg.content
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolResultBlock):
                            self._record_tool_result(block)

            elif isinstance(msg, ResultMessage):
                summary = TurnSummary(
                    num_turns=msg.num_turns,
                    usage=msg.usage,
                    total_cost_usd=msg.total_cost_usd,
                    terminal_reason=msg.terminal_reason,
                    session_id=msg.session_id,
                    is_error=msg.is_error,
                    subtype=msg.subtype,
                )
                self.records.append(
                    "result",
                    {
                        "num_turns": msg.num_turns,
                        "usage": msg.usage,
                        "total_cost_usd": msg.total_cost_usd,
                        "terminal_reason": msg.terminal_reason,
                        "session_id": msg.session_id,
                        "is_error": msg.is_error,
                        "subtype": msg.subtype,
                    },
                )

        return summary

    def _record_tool_result(self, block: ToolResultBlock) -> None:
        content = block.content
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        data: dict[str, Any] = {
            "tool_use_id": block.tool_use_id,
            "is_error": block.is_error,
            "bytes": len(text),
            "summary": text[:ARTIFACT_INLINE_LIMIT],
        }
        if len(text) > ARTIFACT_INLINE_LIMIT:
            path = self.records.artifact_path(self.records.next_seq, "tool_result")
            path.write_text(text, encoding="utf-8")
            data["artifact"] = str(path)
        self.records.append("tool_result", data)

    async def interrupt(self) -> None:
        if self.client is not None:
            await self.client.interrupt()
        self.records.append("interrupt", {})

    async def set_model(self, model: str | None) -> None:
        if self.client is not None:
            await self.client.set_model(model)
        self.state.model = model
        self.cfg.claude.model = model or ""

    async def set_mode(self, mode: str) -> None:
        if self.client is not None:
            await self.client.set_permission_mode(mode)
        self.state.mode = mode
        self.cfg.claude.permission_mode = mode

    async def reconnect(self, **changes: Any) -> None:
        """Disconnect, apply `changes` to state/cfg, reconnect resumed at
        the (possibly just-changed) Claude session id."""
        resume_target = changes.pop("claude_session", None) or self.state.claude_session
        await self.close()
        for key, value in changes.items():
            if hasattr(self.state, key):
                setattr(self.state, key, value)
            if hasattr(self.cfg.claude, key):
                setattr(self.cfg.claude, key, value)
        await self.start(resume=resume_target)
        if resume_target:
            self.state.claude_session = resume_target

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
