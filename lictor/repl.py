"""Input classification for the lictor REPL.

``classify`` is looked up late by callers (``repl.classify(line)``, not a
bound import) so a future ``self.redefine`` can swap it out from under a
running session.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any


@dataclass
class Submission:
    """Something to send to Claude as a user turn."""

    text: str
    to: str | None = None
    source: str = "prose"


@dataclass
class Command:
    """A ``/name args`` line."""

    name: str
    args: str = ""


@dataclass
class ImageForm:
    """A ``!expr`` line, or the joined body of a ``!!`` ... ``!!`` block."""

    src: str


def classify(line: str) -> Command | ImageForm | Submission | None:
    """Classify one input line (or, for a ``!!`` block, the lines the REPL
    loop collected between the two bare ``!!`` delimiters, joined with
    ``"\\n"`` -- the embedded newline is exactly what tells this function
    it is looking at a block rather than a single line).

    | input                      | result                                |
    |-----------------------------|----------------------------------------|
    | ``" ..."`` (leading space) | ``Submission`` of the rest, verbatim   |
    | ``""`` / whitespace only   | ``None``                               |
    | ``"/cmd args"``            | ``Command("cmd", "args")``             |
    | ``"!expr"``                | ``ImageForm("expr")``                  |
    | multi-line (joined block)  | ``ImageForm(whole block)``             |
    | anything else              | ``Submission(line)``                   |
    """
    if "\n" in line:
        # Only the REPL loop's !! ... !! block assembly produces embedded
        # newlines; a single input() line never contains one. The
        # leading-space escape still applies to the block as a whole.
        if not line.strip():
            return None
        if line.startswith(" "):
            return Submission(line[1:])
        return ImageForm(line)

    if not line.strip():
        return None

    if line.startswith(" "):
        return Submission(line[1:])

    if line.startswith("/"):
        rest = line[1:]
        parts = rest.split(None, 1)
        name = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""
        return Command(name, args)

    if line.startswith("!"):
        return ImageForm(line[1:])

    return Submission(line)


def eval_image(src: str, namespace: dict[str, Any]) -> Any:
    """Exec every statement in ``src`` but the last, then if the last
    statement is a bare expression, evaluate and return it (``None``
    otherwise). Runs in ``namespace`` as both globals and locals, so
    assignments persist across calls that share the same dict."""
    tree = ast.parse(src, mode="exec")
    if not tree.body:
        return None

    *body, last = tree.body
    if body:
        module = ast.Module(body=body, type_ignores=[])
        ast.fix_missing_locations(module)
        exec(compile(module, "<image>", "exec"), namespace)  # noqa: S102

    if isinstance(last, ast.Expr):
        expr = ast.Expression(body=last.value)
        ast.fix_missing_locations(expr)
        return eval(compile(expr, "<image>", "eval"), namespace)

    tail = ast.Module(body=[last], type_ignores=[])
    ast.fix_missing_locations(tail)
    exec(compile(tail, "<image>", "exec"), namespace)  # noqa: S102
    return None


#: Commands wired up in wave 1 (dispatch lives in app.py; this is the set
#: app.py uses to tell "unknown" apart from "not available until a later
#: wave" when printing /help).
WAVE1_COMMANDS = frozenset(
    {"help", "quit", "exit", "status", "model", "effort", "mode", "resume", "sessions", "cost"}
)

#: Every command named in the full plan, so an unimplemented one gets its
#: own name back in the "not available until a later wave" message instead
#: of being lumped in with a typo.
ALL_COMMANDS = WAVE1_COMMANDS | frozenset(
    {
        "vault",
        "vault-restore",
        "vault-discard",
        "trace",
        "generations",
        "rollback",
        "diff",
        "commit",
        "discard",
        "tools",
        "worktree",
        "py",
        "mcp",
    }
)
