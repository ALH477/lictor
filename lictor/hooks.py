"""PreToolUse fences and the PostToolUse journal entry: ``build_hooks(ctx)``.

Every callback here is a plain function of ``(input, tool_use_id,
hook_context)`` -- the shape the SDK's ``HookMatcher`` expects -- closing
over lictor's own :class:`lictor.tools.ToolContext` (``ctx``) rather than
the SDK's ``HookContext`` (the third positional argument, which none of
these need and which is therefore named ``_hook_ctx``).

Nothing here may ever raise: a hook callback that raises breaks the turn
it was meant to guard, which is worse than letting the one call through.
Every callback's body runs inside a ``try/except Exception`` that falls
back to ``{}`` (allow / no-op) and records an ``error`` line instead.
"""

from __future__ import annotations

import contextlib
import json
import re
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

_EDIT_MATCHER = "Edit|Write|MultiEdit|NotebookEdit"

#: One compiled pattern per Bash rule, each with its own denial reason so
#: the message says exactly which rule fired.
_BASH_DENY_RULES: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"--no-verify"),
        "guard_bash: --no-verify is refused (CLAUDE.md: never skip hooks)",
    ),
    (
        re.compile(r"\bgit\s+push\b"),
        "guard_bash: git push is refused through this agent",
    ),
    (
        re.compile(r"\brm\s+-rf\s+/(\s|$)"),
        "guard_bash: rm -rf / is refused outright",
    ),
    (
        re.compile(r"\bgit\s+worktree\s+remove\b[^\n]*--force"),
        "guard_bash: git worktree remove --force is refused",
    ),
]

_GIT_COMMIT_RE = re.compile(r"\bgit\s+commit\b")


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _fence_edits_body(ctx: Any, inp: dict[str, Any]) -> dict[str, Any]:
    tool_input = inp.get("tool_input") or {}
    raw_path = tool_input.get("file_path")
    if not raw_path:
        return {}
    path = Path(raw_path)
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path

    state_dir = Path(ctx.paths.state).resolve()
    try:
        if resolved.is_relative_to(state_dir):
            return {}
    except ValueError:
        pass

    worktree = ctx.state.active_worktree
    if worktree is not None:
        try:
            if resolved.is_relative_to(Path(worktree).resolve()):
                return {}
        except ValueError:
            pass

    reason = (
        f"edits are fenced to the active worktree ({worktree if worktree else 'none'}); "
        'run exs.worktree(op="new", name=...) to create one, then edit inside it'
    )
    return _deny(reason)


def _guard_bash_body(ctx: Any, inp: dict[str, Any]) -> dict[str, Any]:
    tool_input = inp.get("tool_input") or {}
    command = str(tool_input.get("command", ""))

    for pattern, reason in _BASH_DENY_RULES:
        if pattern.search(command):
            return _deny(reason)

    if _GIT_COMMIT_RE.search(command) and ctx.state.active_worktree is None:
        return _deny(
            "guard_bash: git commit is refused in the main checkout -- it is shared with "
            'other sessions; run exs.worktree(op="new", name=...) first'
        )

    return {}


def _response_length(response: Any) -> int:
    if isinstance(response, (str, bytes)):
        return len(response)
    try:
        return len(json.dumps(response, ensure_ascii=False, default=str))
    except TypeError:
        return len(str(response))


def _record_tool_result_body(ctx: Any, inp: dict[str, Any], tool_use_id: Any) -> dict[str, Any]:
    ctx.records.append(
        "hook_tool_result",
        {
            "tool_name": inp.get("tool_name"),
            "tool_use_id": tool_use_id,
            "length": _response_length(inp.get("tool_response")),
        },
    )
    return {}


def build_hooks(ctx: Any) -> dict[str, list[HookMatcher]]:
    async def fence_edits(inp: Any, tool_use_id: Any, _hook_ctx: Any) -> dict[str, Any]:
        try:
            return _fence_edits_body(ctx, inp)
        except Exception as exc:  # noqa: BLE001 -- a hook must never raise
            ctx.records.append("error", {"where": "fence_edits", "message": repr(exc)})
            return {}

    async def guard_bash(inp: Any, tool_use_id: Any, _hook_ctx: Any) -> dict[str, Any]:
        try:
            return _guard_bash_body(ctx, inp)
        except Exception as exc:  # noqa: BLE001
            ctx.records.append("error", {"where": "guard_bash", "message": repr(exc)})
            return {}

    async def record_tool_result(inp: Any, tool_use_id: Any, _hook_ctx: Any) -> dict[str, Any]:
        try:
            return _record_tool_result_body(ctx, inp, tool_use_id)
        except Exception as exc:  # noqa: BLE001
            with contextlib.suppress(Exception):  # the journal itself must not break the turn
                ctx.records.append("error", {"where": "record_tool_result", "message": repr(exc)})
            return {}

    return {
        "PreToolUse": [
            HookMatcher(matcher=_EDIT_MATCHER, hooks=[fence_edits], timeout=30),
            HookMatcher(matcher="Bash", hooks=[guard_bash], timeout=30),
        ],
        "PostToolUse": [
            HookMatcher(matcher=None, hooks=[record_tool_result], timeout=15),
        ],
    }
