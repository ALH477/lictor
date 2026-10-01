"""Programmatic subagent roles, loaded from Markdown + front matter.

A role file is Markdown with YAML-*ish* front matter between two bare
``---`` fences; the body after the closing fence is the subagent's prompt.
This module does **not** depend on PyYAML -- the front matter grammar
lictor actually needs is small enough that a hand-rolled parser is the
honest choice rather than pulling in a dependency the project rule
("stdlib + claude-agent-sdk only", see the M5 plan) does not otherwise
need:

    ---
    name: spec-guardian
    description: Checks a claim against the spec and the §13 registry.
    model: sonnet
    tools: Read, Grep, Glob
    max_turns: 8
    effort: low
    ---
    <prompt body, verbatim>

Recognised keys: ``name`` (defaults to the filename stem), ``description``
(**required** -- a file missing it is skipped, with a warning logged, not
raised: one bad role file must not take the whole roster down),
``model``, ``tools`` (comma-separated, or a YAML-style ``[a, b, c]``
list), ``max_turns``, ``effort``. Any other key is ignored; this is
reported via a logged line (not a second return value -- ``load()``'s
single ``dict[str, AgentDefinition]`` return value is what every caller
in brain.py/options.py actually wants, and a role file with a stray key
is a warning, not something a caller needs to branch on).

Search order, later overriding earlier **by role name** (not by file):
``<paths.config>/agents/*.md``, then
``<cfg.workspace.path>/.lictor/agents/*.md``. The repository's own
``.claude/agents/*.md`` (Exsecutor's 8 scoped subagents) are deliberately
not read here -- those load natively through Claude Code's own
``setting_sources``, per the plan; this module is only for the
*programmatic* roster passed as ``ClaudeAgentOptions(agents=...)``.

``AgentDefinition`` fields, as actually checked against the installed
SDK (0.2.163, ``claude_agent_sdk.AgentDefinition``)::

    description, prompt, tools, disallowedTools, model, skills, memory,
    mcpServers, initialPrompt, maxTurns, background, effort, permissionMode

Both ``max_turns`` -> ``maxTurns`` and ``effort`` -> ``effort`` exist on
this installed version, so neither is dropped: the plan's instruction to
"drop silently... for any it does not" turned out to have nothing to do,
and that is recorded here rather than silently assumed. ``name`` is not an
``AgentDefinition`` field at all -- it is the key lictor uses in the
``dict[str, AgentDefinition]`` that ``ClaudeAgentOptions(agents=...)``
wants, so a role file's ``name:`` (or its filename stem) becomes that
dict key, never a constructor argument.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Front-matter keys this parser understands. Anything else is ignored,
#: with a logged line -- see the module docstring for why that is the
#: second-return-value alternative the plan offered.
_RECOGNISED_KEYS = frozenset({"name", "description", "model", "tools", "max_turns", "effort"})


def _parse_list(raw: str) -> list[str]:
    """``"a, b, c"`` or ``"[a, b, c]"`` -> ``["a", "b", "c"]``."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_front_matter(text: str) -> tuple[dict[str, str], str] | None:
    """Split a role file into its front-matter dict (raw string values,
    one ``key: value`` per line) and body. ``None`` if the file does not
    open with a bare ``---`` line."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None

    front: dict[str, str] = {}
    i = 1
    while i < len(lines) and lines[i].strip() != "---":
        line = lines[i]
        if ":" in line and line.strip():
            key, _, value = line.partition(":")
            front[key.strip()] = value.strip()
        i += 1

    if i >= len(lines):
        # Opening fence with no closing fence: malformed, not a role.
        return None

    body = "\n".join(lines[i + 1 :]).strip("\n")
    return front, body


def _build_one(path: Path) -> tuple[str, Any] | None:
    """Parse one role file into ``(name, AgentDefinition)``, or ``None`` if
    the file is skipped (no front matter, or no ``description``) -- either
    case is logged as a warning rather than raised."""
    from claude_agent_sdk import AgentDefinition

    text = path.read_text(encoding="utf-8")
    parsed = _parse_front_matter(text)
    if parsed is None:
        logger.warning("roles: %s has no '---' front matter; skipped", path)
        return None
    front, body = parsed

    description = front.get("description")
    if not description:
        logger.warning("roles: %s has no 'description'; skipped (a subagent needs one so the "
                        "orchestrator knows when to delegate to it)", path)
        return None

    name = front.get("name") or path.stem

    unknown = sorted(set(front) - _RECOGNISED_KEYS)
    if unknown:
        logger.info("roles: %s: unrecognised front-matter keys ignored: %s", path, unknown)

    kwargs: dict[str, Any] = {"description": description, "prompt": body}

    if "tools" in front:
        kwargs["tools"] = _parse_list(front["tools"])
    if "model" in front:
        kwargs["model"] = front["model"]
    if "max_turns" in front:
        try:
            kwargs["maxTurns"] = int(front["max_turns"])
        except ValueError:
            logger.warning("roles: %s: max_turns=%r is not an integer; ignored", path, front["max_turns"])
    if "effort" in front:
        kwargs["effort"] = front["effort"]

    return name, AgentDefinition(**kwargs)


def _role_dirs(cfg: Any, paths: Any) -> list[Path]:
    return [Path(paths.config) / "agents", Path(cfg.workspace.path) / ".lictor" / "agents"]


def load(cfg: Any, paths: Any) -> dict[str, Any]:
    """Build the programmatic subagent roster: ``{role_name:
    AgentDefinition}``, suitable to pass straight to
    ``ClaudeAgentOptions(agents=...)`` (see ``options.build_options``).

    Searched in order, **later sources overriding earlier ones by role
    name** (so a workspace's ``.lictor/agents/spec-guardian.md`` can
    replace the user's own, but two files that define the same name in
    the same directory just resolve by whichever ``sorted(glob())`` order
    picks last -- role files should not collide within one directory):

        1. ``<paths.config>/agents/*.md``   (user-wide)
        2. ``<cfg.workspace.path>/.lictor/agents/*.md``   (per-workspace)

    A directory that does not exist is skipped silently (most installs
    have neither). A file that fails to parse is skipped with a logged
    warning -- never raised, since one bad role file should not prevent
    lictor from starting.
    """
    roles: dict[str, Any] = {}
    for directory in _role_dirs(cfg, paths):
        if not directory.is_dir():
            continue
        for role_path in sorted(directory.glob("*.md")):
            result = _build_one(role_path)
            if result is None:
                continue
            name, definition = result
            roles[name] = definition
    return roles


def summary(roles: dict[str, Any]) -> str:
    """One line per role, ``name: first line of description`` -- what
    ``options.py`` folds into the system prompt append so the model knows
    what it can delegate to via the Agent tool. ``"none"`` if the roster
    is empty."""
    if not roles:
        return "none"
    lines = []
    for name, definition in roles.items():
        description = definition.description or ""
        first_line = description.splitlines()[0] if description else ""
        lines.append(f"{name}: {first_line}")
    return "\n".join(lines)
