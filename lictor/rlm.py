"""Bounded recursive inference: ``rlm.infer`` / ``rlm.map`` / ``rlm.complete``.

Each call is a **separate** ``claude_agent_sdk.query()`` run -- not the
session's ``ClaudeSDKClient`` -- with no tools, no memory, and
``system_prompt`` fixed to ``prompts/rlm.md``. It exists so a running
conversation can spend a bounded number of turns asking a tool-less
sub-model a narrow question against explicit context, without that
sub-call ever touching the main session or the filesystem beyond the
context it was handed.

Every call is metered against a shared :class:`Budget` (turns, not
dollars) and leaves a :class:`Trace` behind at
``<data>/inferences/<id>.json`` so ``resource.read("inference:<id>")``
and ``/trace ID`` can show what happened.

``claude_agent_sdk`` is imported at module level: by the time anything
imports ``lictor.rlm`` the launcher has already run ``scrub_environ()``
(see ``options.py`` and ``__main__.py``), same as ``brain.py``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

from . import ollama as ollama_mod

#: A context file bigger than this is refused outright, never silently
#: truncated -- see _load_context.
MAX_CONTEXT_FILE_BYTES = 256 * 1024

_PROMPT_PATH = Path(__file__).parent / "prompts" / "rlm.md"


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class BudgetExhausted(Exception):
    """Raised by ``Budget.take`` when there are not enough turns left."""

    def __init__(self, requested: int, left: int) -> None:
        super().__init__(f"rlm budget exhausted: requested {requested} turn(s), {left} left")
        self.requested = requested
        self.left = left


class ContextTooLarge(Exception):
    """Raised by ``infer``/``map_`` when a context file is over the cap.

    Refused rather than truncated: a silently-truncated file could make
    the sub-model answer confidently about data it never actually saw.
    """

    def __init__(self, path: Path, size: int) -> None:
        super().__init__(
            f"context file {path} is {size} bytes, over the {MAX_CONTEXT_FILE_BYTES}-byte cap; "
            "refused rather than truncated"
        )
        self.path = path
        self.size = size


@dataclass
class Budget:
    """A shared turn budget. ``used`` is mutated in place by ``take``."""

    total_turns: int
    used: int = 0

    def remaining(self) -> int:
        return self.total_turns - self.used

    def take(self, n: int = 1) -> None:
        left = self.remaining()
        if n > left:
            raise BudgetExhausted(n, left)
        self.used += n


@dataclass
class Trace:
    """Exactly what gets written to ``<data>/inferences/<id>.json``.

    ``error`` is an addition beyond the plan's listed key set, carrying
    a failed call's exception so ``map_`` can let one bad item fail
    without losing the record of *why* -- see the M4 report for the
    note. Every other key matches the plan verbatim.
    """

    id: str
    parent_cid: str | None
    t: str
    model: str | None
    effort: str
    prompt: str
    context: list[dict[str, Any]]
    schema: dict[str, Any] | None
    result: str | None
    structured_output: Any
    usage: dict[str, Any] | None
    session_id: str | None
    ms: int
    budget: dict[str, int]
    error: dict[str, str] | None = field(default=None)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _make_id() -> str:
    now = dt.datetime.now(dt.UTC)
    return f"inf-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"


def _load_context(items: Any) -> tuple[str, list[dict[str, Any]]]:
    """Read every context path, hashing and size-checking each one.

    Returns the text to inline into the prompt (one fenced block per
    file, headed by its path/bytes/sha256) and the ``[{path,sha256,bytes}]``
    list that goes straight into the Trace.
    """
    blocks: list[str] = []
    files: list[dict[str, Any]] = []
    for raw in items:
        path = Path(raw)
        data = path.read_bytes()
        if len(data) > MAX_CONTEXT_FILE_BYTES:
            raise ContextTooLarge(path, len(data))
        digest = hashlib.sha256(data).hexdigest()
        files.append({"path": str(path), "sha256": digest, "bytes": len(data)})
        text = data.decode("utf-8", errors="replace")
        blocks.append(f"--- context: {path} ({len(data)} bytes, sha256 {digest}) ---\n{text}")
    return "\n\n".join(blocks), files


def _write_trace(paths: Any, trace: Trace) -> Path:
    out_dir = paths.data / "inferences"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{trace.id}.json"
    tmp = out_path.with_name(out_path.name + f".tmp-{os.getpid()}")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(trace.to_dict(), fh, ensure_ascii=False, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out_path)
    return out_path


def _workspace_cwd(ctx: Any) -> str:
    state = getattr(ctx, "state", None)
    workspace = getattr(state, "workspace", None)
    if workspace:
        return str(workspace)
    return str(getattr(ctx.cfg.workspace, "path", "."))


async def infer(
    ctx: Any,
    prompt: str,
    context: Any = (),
    schema: dict[str, Any] | None = None,
    model: str | None = None,
    effort: str = "low",
    max_turns: int = 1,
) -> Trace:
    """Run one bounded, tool-less, memory-less inference call.

    `ctx` is a ``tools.ToolContext`` (or anything that quacks like one):
    ``ctx.budget`` is the shared :class:`Budget`, ``ctx.cfg.rlm`` the
    default model, ``ctx.paths`` where the Trace lands, ``ctx.state``
    (optional) the calling conversation's id/workspace.

    Raises :class:`ContextTooLarge` or :class:`BudgetExhausted` outright
    -- both are refusals, not failures of the call itself. A failure of
    the underlying ``query()`` call (the sub-model erroring, the CLI
    misbehaving, ...) is instead caught and recorded on the Trace's
    ``error`` field, so a batch under ``map_`` survives it.
    """
    context_text, context_files = _load_context(context)

    budget: Budget = ctx.budget
    before = budget.used
    budget.take(max_turns)

    rlm_cfg = ctx.cfg.rlm
    effective_model = model or (getattr(rlm_cfg, "model", None) or None)
    ollama_cfg = getattr(ctx.cfg, "ollama", None)
    if ollama_cfg is not None and ollama_cfg.enabled:
        # The local server has no "haiku"; run on the one Ollama model.
        effective_model = ollama_mod.effective_model(ctx.cfg) or effective_model

    full_prompt = prompt if not context_text else f"{prompt}\n\n{context_text}"
    options_kwargs: dict[str, Any] = {
        "system_prompt": _PROMPT_PATH.read_text(encoding="utf-8"),
        "tools": [],
        "allowed_tools": [],
        "permission_mode": "dontAsk",
        "setting_sources": [],
        "max_turns": max_turns,
        "cwd": _workspace_cwd(ctx),
        "cli_path": ctx.cfg.resolve_cli_path(),
        "model": effective_model,
        "effort": effort,
    }
    backend = ollama_mod.backend_env(ctx.cfg) if ollama_cfg is not None else {}
    if backend:
        options_kwargs["env"] = backend
    if schema is not None:
        options_kwargs["output_format"] = {"type": "json_schema", "schema": schema}
    options = ClaudeAgentOptions(**options_kwargs)

    t0 = time.monotonic()
    result_text: str | None = None
    structured_output: Any = None
    usage: dict[str, Any] | None = None
    session_id: str | None = None
    error: dict[str, str] | None = None
    try:
        async for msg in query(prompt=full_prompt, options=options):
            if isinstance(msg, ResultMessage):
                result_text = msg.result
                structured_output = msg.structured_output
                usage = msg.usage
                session_id = msg.session_id
    except Exception as exc:  # noqa: BLE001 - recorded on the Trace, not swallowed
        error = {"type": type(exc).__name__, "message": str(exc)}
    ms = int((time.monotonic() - t0) * 1000)

    state = getattr(ctx, "state", None)
    trace = Trace(
        id=_make_id(),
        parent_cid=getattr(state, "cid", None),
        t=_now_iso(),
        model=effective_model,
        effort=effort,
        prompt=prompt,
        context=context_files,
        schema=schema,
        result=result_text,
        structured_output=structured_output,
        usage=usage,
        session_id=session_id,
        ms=ms,
        budget={"before": before, "after": budget.used},
        error=error,
    )
    _write_trace(ctx.paths, trace)
    return trace


async def map_(
    ctx: Any,
    prompt: str,
    items: list[Any],
    concurrency: int | None = None,
    **kw: Any,
) -> list[Trace]:
    """Run ``infer`` once per item, ``concurrency`` at a time.

    Each item is appended to `prompt` as its own block. A failed item
    (the underlying ``query()`` raising) does not fail the batch -- its
    Trace just carries an ``error`` -- but a refusal (``BudgetExhausted``
    or ``ContextTooLarge``) still propagates, since those mean the batch
    as configured cannot proceed at all.
    """
    n = concurrency if concurrency else getattr(ctx.cfg.rlm, "concurrency", 3)
    sem = asyncio.Semaphore(max(1, n))

    async def _one(item: Any) -> Trace:
        async with sem:
            item_prompt = f"{prompt}\n\n--- item ---\n{item}"
            return await infer(ctx, item_prompt, **kw)

    return await asyncio.gather(*[_one(item) for item in items])
