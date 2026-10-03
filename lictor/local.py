"""The local subagent: a small fine-tuned model (default a 3B Exsecutor
tune served by Ollama) that Claude leads.

This is deliberately *not* the Claude Code subprocess. ``local.ask`` is a
plain, tool-less, one-shot ``/api/chat`` call to Ollama, so it works
whether the main session runs on the subscription or on Ollama too
(``lictor/ollama.py``). The model never sees the conversation, has no
tools, and can touch nothing but the text it returns; what it gets is the
prompt plus any workspace files Claude names.

Its output is **unverified by construction**: a 3B tune is fast and cheap
and sometimes confidently wrong. Every result is marked ``unverified`` and
Claude, which leads, is expected to check it (against the spec, the §13
registry, or by running the gate) before relying on it. The text it returns
is data, never instructions to Claude.

Every call leaves a trace at ``<data>/inferences/loc-*.json``, readable
with ``resource.read("inference:<id>")``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from . import ollama as ollama_mod

#: A context file bigger than this is refused, never truncated -- a 3B
#: model's window is small and a silent cut would make it answer about
#: text it never saw.
MAX_CONTEXT_FILE_BYTES = 64 * 1024

SYSTEM = (
    "You are a small local assistant for the Exsecutor compiler project, working "
    "for a lead engineer who will check your answer. Answer only from the text "
    "you are given. If it does not contain the answer, say so. Be terse. Never "
    "invent an EXS-E error code."
)


class LocalError(Exception):
    """A refusal or failure of a local call, reported to Claude as data."""


def host(cfg: Any) -> str:
    return (cfg.local.host or cfg.ollama.host).rstrip("/")


def _workspace(ctx: Any) -> Path:
    return Path(getattr(getattr(ctx, "state", None), "workspace", None) or ctx.cfg.workspace.path).resolve()


def load_context(ctx: Any, items: Any) -> tuple[str, list[dict[str, Any]]]:
    """Read workspace files into one prompt block each. Paths outside the
    workspace are refused: the operator's other files are not the
    subagent's business."""
    root = _workspace(ctx)
    blocks: list[str] = []
    files: list[dict[str, Any]] = []
    for raw in items:
        path = (root / raw).resolve()
        if not path.is_relative_to(root):
            raise LocalError(f"context {raw!r} is outside the workspace; refused")
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise LocalError(f"context {raw!r} could not be read: {exc}") from exc
        if len(data) > MAX_CONTEXT_FILE_BYTES:
            raise LocalError(
                f"context {raw!r} is {len(data)} bytes, over the {MAX_CONTEXT_FILE_BYTES}-byte "
                "cap; refused rather than truncated"
            )
        digest = hashlib.sha256(data).hexdigest()
        files.append({"path": str(path), "sha256": digest, "bytes": len(data)})
        text = data.decode("utf-8", errors="replace")
        blocks.append(f"--- context: {raw} ({len(data)} bytes) ---\n{text}")
    return "\n\n".join(blocks), files


def _chat_sync(url: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(  # noqa: S310 - operator-configured host
        url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise LocalError(f"ollama returned HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise LocalError(f"cannot reach Ollama at {url} ({exc}); is `ollama serve` running?") from exc


def _write_trace(paths: Any, trace: dict[str, Any]) -> Path:
    out_dir = paths.data / "inferences"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{trace['id']}.json"
    tmp = out.with_name(out.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(trace, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out


async def ask(
    ctx: Any,
    prompt: str,
    context: Any = (),
    schema: dict[str, Any] | None = None,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    """One tool-less call to the local model. Returns the trace dict.

    Raises :class:`LocalError` for a refusal (disabled, call cap, bad
    context). A failed HTTP call is raised too -- there is no batch here
    for it to survive, so Claude is simply told."""
    cfg = ctx.cfg.local
    if not cfg.enabled:
        raise LocalError("local subagent is disabled: set [local].enabled = true in config.toml")
    if ctx.local_calls >= cfg.max_calls:
        raise LocalError(f"local call cap reached ({cfg.max_calls} this session)")
    context_text, context_files = load_context(ctx, context)
    ctx.local_calls += 1

    body: dict[str, Any] = {
        "model": cfg.model,
        "stream": False,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": prompt if not context_text else f"{context_text}\n\n{prompt}"},
        ],
        "options": {"temperature": 0, "num_predict": max_tokens or cfg.max_tokens},
    }
    if schema is not None:
        body["format"] = schema

    t0 = time.monotonic()
    reply = await asyncio.to_thread(_chat_sync, host(ctx.cfg) + "/api/chat", body, cfg.timeout_s)
    ms = int((time.monotonic() - t0) * 1000)
    text = (reply.get("message") or {}).get("content", "")

    structured: Any = None
    if schema is not None:
        try:
            structured = json.loads(text)
        except ValueError:
            structured = None

    now = dt.datetime.now(dt.UTC)
    trace = {
        "id": f"loc-{now.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}",
        "kind": "local",
        "parent_cid": getattr(getattr(ctx, "state", None), "cid", None),
        "t": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "model": cfg.model,
        "prompt": prompt,
        "context": context_files,
        "schema": schema,
        "result": text,
        "structured_output": structured,
        "unverified": True,
        "usage": {"prompt_tokens": reply.get("prompt_eval_count"), "output_tokens": reply.get("eval_count")},
        "ms": ms,
        "calls": {"used": ctx.local_calls, "cap": cfg.max_calls},
    }
    _write_trace(ctx.paths, trace)
    return trace


async def status(ctx: Any) -> dict[str, Any]:
    cfg = ctx.cfg.local
    row: dict[str, Any] = {"enabled": cfg.enabled, "model": cfg.model, "host": host(ctx.cfg),
                           "calls": {"used": ctx.local_calls, "cap": cfg.max_calls}}
    try:
        models = await asyncio.to_thread(ollama_mod.list_models, host(ctx.cfg))
        row["reachable"] = True
        row["model_present"] = ollama_mod._has(models, cfg.model)
    except ollama_mod.OllamaError as exc:
        row["reachable"] = False
        row["error"] = str(exc)
    return row
