"""Environment scrubbing, session State, and ClaudeAgentOptions construction.

``claude_agent_sdk`` is imported lazily, inside ``build_options`` only, so
that ``scrub_environ()`` can run (from ``__main__``) before the SDK --
and whatever it reads at import time -- ever sees the unscrubbed
environment.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Keys removed outright regardless of prefix.
SCRUB_KEYS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "ANTHROPIC_API_KEY")
#: Prefix removed unless the key is in SCRUB_KEEP.
SCRUB_PREFIXES = ("CLAUDE_CODE_",)
#: Never removed, even though they match a scrubbed prefix/key.
SCRUB_KEEP = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")


def scrub_environ(env: dict[str, str] | None = None) -> list[str]:
    """Delete the ``CLAUDE_CODE_*`` / ``CLAUDECODE`` / ``CLAUDE_PID`` /
    ``CLAUDE_EFFORT`` / ``ANTHROPIC_API_KEY`` keys from `env` (``os.environ``
    by default), keeping ``CLAUDE_CODE_OAUTH_TOKEN`` and
    ``CLAUDE_CONFIG_DIR``. Must run before ``claude_agent_sdk`` (or anything
    that imports it) is imported. Returns the names actually removed."""
    target = os.environ if env is None else env
    removed: list[str] = []
    for key in list(target):
        if key in SCRUB_KEEP:
            continue
        if key in SCRUB_KEYS or any(key.startswith(prefix) for prefix in SCRUB_PREFIXES):
            del target[key]
            removed.append(key)
    return removed


@dataclass
class State:
    """Everything about the running session that options/brain/app/repl
    need to agree on. Mutated in place as the session progresses."""

    cwd: Path
    workspace: Path
    active_worktree: Path | None
    cid: str
    claude_session: str | None = None
    model: str | None = None
    effort: str = "medium"
    mode: str = "default"


#: Built-in tools lictor allows without an approval prompt, before any
#: mcp__<ns>__* tools a later wave adds.
BASE_ALLOWED_TOOLS = ["Read", "Glob", "Grep", "TodoWrite", "Agent", "WebFetch"]

_PROMPT_PATH = Path(__file__).parent / "prompts" / "lictor.md"


def _system_prompt_append(state: State) -> str:
    base = _PROMPT_PATH.read_text(encoding="utf-8")
    worktree = str(state.active_worktree) if state.active_worktree else "none"
    return f"{base}\n\nWorkspace: {state.workspace}\nActive worktree: {worktree}\n"


def build_options(
    cfg: Any,
    state: State,
    *,
    resume: str | None = None,
    fresh_cid: str | None = None,
    hooks: Any = None,
    mcp_servers: Any = None,
    can_use_tool: Callable[..., Any] | None = None,
    extra_allowed_tools: list[str] | None = None,
) -> Any:
    """Build a ``ClaudeAgentOptions`` for ``cfg``/``state``.

    Exactly one of ``resume`` / ``fresh_cid`` may be given (never both):
    ``resume`` continues an existing Claude session id, ``fresh_cid`` starts
    a new one pinned to that id so lictor's conversation id and the
    underlying Claude session id agree from turn one.
    """
    from claude_agent_sdk import ClaudeAgentOptions

    if resume and fresh_cid:
        raise ValueError("build_options: resume and fresh_cid are mutually exclusive")

    allowed_tools = list(BASE_ALLOWED_TOOLS)
    if extra_allowed_tools:
        allowed_tools.extend(extra_allowed_tools)

    kwargs: dict[str, Any] = {
        "system_prompt": {
            "type": "preset",
            "preset": "claude_code",
            "append": _system_prompt_append(state),
        },
        "setting_sources": cfg.claude.setting_sources,
        "cwd": str(state.cwd),
        "cli_path": cfg.resolve_cli_path(),
        "include_partial_messages": True,
        "permission_mode": cfg.claude.permission_mode,
        "model": cfg.claude.model or None,
        "effort": cfg.claude.effort,
        "env": {"MCP_TIMEOUT": "120000"},
        "allowed_tools": allowed_tools,
    }
    if hooks is not None:
        kwargs["hooks"] = hooks
    if mcp_servers is not None:
        kwargs["mcp_servers"] = mcp_servers
    if can_use_tool is not None:
        kwargs["can_use_tool"] = can_use_tool

    if resume:
        kwargs["resume"] = resume
    elif fresh_cid:
        # The plan's contract calls for extra_args={"session-id": fresh_cid};
        # this installed SDK (0.2.163) has a native `session_id` dataclass
        # field that does the same thing directly, so that's used instead --
        # see the M1 report for the full note.
        kwargs["session_id"] = fresh_cid

    if hasattr(ClaudeAgentOptions, "verbatim_prompts"):
        kwargs["verbatim_prompts"] = True

    return ClaudeAgentOptions(**kwargs)
