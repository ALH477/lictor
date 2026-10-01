"""Approval bridge between the Agent SDK's ``can_use_tool`` callback (runs on
the asyncio loop thread) and an interactive prompt on the main thread.

``can_use_tool`` enqueues a ``(Request, concurrent.futures.Future)`` pair and
awaits the future via ``asyncio.wrap_future`` -- cheap and thread-safe, since
``Future.set_result`` from another thread just schedules the loop-thread
awaiter's wakeup. ``service_pending()`` is the main thread's side: it drains
one request, prints it, and blocks on ``input()`` for a decision.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
from dataclasses import dataclass
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny


@dataclass
class Request:
    tool_name: str
    input_data: dict[str, Any]
    context: Any


def _prefix(tool_name: str, input_data: dict[str, Any]) -> str:
    """The part of the call that `session_allow` keys on: the command's
    argv[0] for Bash, the path for an edit, nothing for anything else."""
    if tool_name == "Bash":
        command = str(input_data.get("command", "")).split()
        return command[0] if command else ""
    if tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return str(input_data.get("file_path", ""))
    return ""


def _summarize(request: Request) -> str:
    if request.tool_name == "Bash":
        return str(request.input_data.get("command", ""))[:120]
    if request.tool_name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return str(request.input_data.get("file_path", ""))
    return str(request.input_data)[:120]


class Approvals:
    def __init__(self) -> None:
        self.session_allow: set[tuple[str, str]] = set()
        self._pending: queue.Queue[tuple[Request, concurrent.futures.Future]] = queue.Queue()

    async def can_use_tool(self, tool_name: str, input_data: dict[str, Any], context: Any) -> Any:
        """The SDK's ``CanUseTool`` callback. Runs on the loop thread."""
        prefix = _prefix(tool_name, input_data)
        if (tool_name, prefix) in self.session_allow:
            return PermissionResultAllow(updated_input=input_data)

        fut: concurrent.futures.Future = concurrent.futures.Future()
        self._pending.put((Request(tool_name, input_data, context), fut))
        return await asyncio.wrap_future(fut)

    def has_pending(self) -> bool:
        return not self._pending.empty()

    def service_pending(self) -> bool:
        """Answer one pending request interactively, on the main thread.
        Returns False if the queue was empty (nothing to do this poll)."""
        try:
            request, fut = self._pending.get_nowait()
        except queue.Empty:
            return False

        if fut.done():
            return True

        print(f"approve {request.tool_name}: {_summarize(request)}")
        try:
            answer = input("[y]es / [n]o / [a]lways this session / [i]nterrupt: ").strip().lower()
        except KeyboardInterrupt:
            fut.set_result(PermissionResultDeny(message="interrupted at approval prompt", interrupt=True))
            return True
        except EOFError:
            answer = "n"

        if answer == "y":
            fut.set_result(PermissionResultAllow(updated_input=request.input_data))
        elif answer == "a":
            self.session_allow.add((request.tool_name, _prefix(request.tool_name, request.input_data)))
            fut.set_result(PermissionResultAllow(updated_input=request.input_data))
        elif answer == "i":
            fut.set_result(PermissionResultDeny(message="denied and interrupted by operator", interrupt=True))
        else:
            fut.set_result(PermissionResultDeny(message="denied by operator"))
        return True
