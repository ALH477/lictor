"""Render SDK messages and stream events to a terminal (or any text stream)."""

from __future__ import annotations

import sys
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_RESET = "\x1b[0m"

_SUMMARY_KEYS = ("command", "file_path", "path", "pattern", "prompt", "query", "url")


def _summarize_input(input_data: dict[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        if key in input_data:
            return str(input_data[key])[:80]
    return str(input_data)[:80]


class Renderer:
    """Stateful: tracks whether this turn's assistant text already printed
    via stream deltas, and the tool_use_id -> tool name mapping, so a
    ToolResultBlock (which carries only the id) can be labelled."""

    def __init__(self, out: Any = None) -> None:
        self.out = out if out is not None else sys.stdout
        self._tty = bool(getattr(self.out, "isatty", lambda: False)())
        self._streamed_text = False
        self._tool_names: dict[str, str] = {}

    def _dim(self, text: str) -> str:
        return f"{_DIM}{text}{_RESET}" if self._tty else text

    def _bold(self, text: str) -> str:
        return f"{_BOLD}{text}{_RESET}" if self._tty else text

    def on_stream(self, ev: StreamEvent) -> None:
        event = ev.event or {}
        if event.get("type") != "content_block_delta":
            return
        delta = event.get("delta") or {}
        if delta.get("type") != "text_delta":
            return
        text = delta.get("text", "")
        if not text:
            return
        self._streamed_text = True
        self.out.write(text)
        self.out.flush()

    def on_message(self, msg: Any) -> None:
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    if not self._streamed_text:
                        self.out.write(block.text)
                        self.out.flush()
                elif isinstance(block, ToolUseBlock):
                    self._tool_names[block.id] = block.name
                    line = f"⟶ {block.name}: {_summarize_input(block.input)}"
                    self.out.write("\n" + self._bold(line) + "\n")
                elif isinstance(block, ThinkingBlock):
                    self.out.write(self._dim("(thinking...)") + "\n")
            return

        if isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        text = block.content if isinstance(block.content, str) else str(block.content)
                        first_line = text.splitlines()[0] if text else ""
                        name = self._tool_names.get(block.tool_use_id, block.tool_use_id)
                        self.out.write(f"⟵ {name} {len(text)} chars {first_line[:80]}\n")
            return

        if isinstance(msg, ResultMessage):
            self._streamed_text = False
            cost = msg.total_cost_usd if msg.total_cost_usd is not None else "none"
            line = f"· turns={msg.num_turns} session={msg.session_id} cost={cost} stop={msg.terminal_reason}"
            self.out.write(self._dim(line) + "\n")
            self.out.flush()
            return

        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            data = msg.data or {}
            tools = data.get("tools") or []
            mcp_servers = data.get("mcp_servers") or data.get("mcpServers") or []
            bits = []
            for server in mcp_servers:
                if isinstance(server, dict):
                    bits.append(f"{server.get('name')}:{server.get('status')}")
                else:
                    bits.append(str(server))
            line = f"· model={data.get('model')} tools={len(tools)} mcp={','.join(bits)}"
            self.out.write(self._dim(line) + "\n")
            for err_key in ("plugin_errors", "mcp_server_errors"):
                errs = data.get(err_key)
                if errs:
                    self.out.write(self._dim(f"· {err_key}: {errs}") + "\n")
            self.out.flush()
            return
