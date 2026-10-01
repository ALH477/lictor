"""`self.*`: the tools that make lictor autolith-like -- inspecting,
redefining, exercising, diffing, discarding, and committing its own live
image, through the overlay journal in `lictor/overlay.py`.

The module is named `self_` because `self` is a Python keyword; the
registry maps the wire namespace back to `self` via
`lictor.tools.MODULE_NAMESPACE` (``NAMESPACE`` below is set for the same
reason every other namespace module sets it, even though the registry
does not actually need it for this one).
"""

from __future__ import annotations

import ast
import difflib
import importlib
import inspect
import json
import re
import time
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from .. import overlay

NAMESPACE = "self"


def _ok(text: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "detail": text}
    payload.update(extra)
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


def _error(text: str, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": False, "detail": text}
    payload.update(extra)
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}], "is_error": True}


def _live_object(target: str, ctx: Any) -> Any:
    """The object that would actually run right now for `target`: a tool's
    live (possibly rebound) handler, or a module attribute's current
    value. `inspect.getsource` on this is exactly "current effective
    source -- overlay if applied, else the tracked definition", since the
    live object literally *is* whichever one is currently bound."""
    kind, rest = overlay.split_target(target)
    if kind == "tool":
        dotted = rest
        if ctx.registry is None or dotted not in ctx.registry.tools:
            raise overlay.RedefineRefused(f"tool:{dotted} is not a registered tool")
        return ctx.registry.tools[dotted].handler
    mod_name, attr = rest.split(":", 1)
    module = importlib.import_module(mod_name)
    return getattr(module, attr)


async def _status(_args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    state = ctx.state
    worktree = getattr(state, "active_worktree", None) if state is not None else None
    payload = {
        "generation": overlay.active_generation(ctx.paths),
        "overlay": [
            {"seq": e.seq, "target": e.target, "status": e.status} for e in overlay.list_entries(ctx.paths)
        ],
        "worktree": str(worktree) if worktree else None,
        "session": getattr(state, "claude_session", None) if state is not None else None,
        "model": getattr(state, "model", None) if state is not None else None,
        "effort": getattr(state, "effort", None) if state is not None else None,
        "tool_count": len(ctx.registry.tools) if ctx.registry is not None else 0,
    }
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}]}


async def _describe(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    target = args["target"]
    try:
        obj = _live_object(target, ctx)
        source = inspect.getsource(obj)
    except overlay.RedefineRefused as exc:
        return _error(str(exc))
    except (OSError, TypeError) as exc:
        return _error(f"describe: could not get source for {target!r}: {exc}")
    return _ok(source)


async def _redefine(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    target, source = args["target"], args["source"]
    try:
        entry = ctx.image.redefine(target, source)
    except overlay.RedefineRefused as exc:
        return _error(str(exc))
    return _ok(
        f"proposed seq {entry.seq} for {target}. A schema change is always refused -- redefining a "
        "tool can change its behavior but never its wire schema, which needs a restart. Nothing here "
        "is durable: exercise it, then self.commit, or it is lost on exit.",
        seq=entry.seq,
    )


async def _diff(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    target = args["target"]
    try:
        tracked_path, tracked_node, tracked_source = overlay.find_definition(target)
        tracked_segment = ast.get_source_segment(tracked_source, tracked_node) or ""
        live_segment = inspect.getsource(_live_object(target, ctx))
    except overlay.RedefineRefused as exc:
        return _error(str(exc))
    except (OSError, TypeError) as exc:
        return _error(f"diff: could not get source for {target!r}: {exc}")

    diff_text = "".join(
        difflib.unified_diff(
            tracked_segment.splitlines(keepends=True),
            live_segment.splitlines(keepends=True),
            fromfile=f"tracked:{tracked_path}",
            tofile=f"live:{target}",
        )
    )
    return _ok(diff_text or "(no difference)")


async def _exercise(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    target = args["target"]
    raw_args_json = args.get("args_json") or "{}"
    try:
        parsed = json.loads(raw_args_json)
    except json.JSONDecodeError as exc:
        return _error(f"exercise: args_json is not valid JSON: {exc}")

    entry = overlay.latest_entry_for_target(ctx.paths, target)
    if entry is None:
        return _error(f"exercise refuses {target!r}: no overlay entry yet -- self.redefine it first")
    if entry.status == "discarded":
        return _error(f"exercise refuses seq {entry.seq}: it was discarded")

    kind, rest = overlay.split_target(target)
    t0 = time.monotonic()
    try:
        if kind == "tool":
            handler = ctx.registry.tools[rest].handler
            value = await handler(parsed if isinstance(parsed, dict) else {})
        else:
            mod_name, attr = rest.split(":", 1)
            fn = getattr(importlib.import_module(mod_name), attr)
            if isinstance(parsed, dict):
                value = fn(**parsed)
            elif isinstance(parsed, list):
                value = fn(*parsed)
            elif parsed is None:
                value = fn()
            else:
                value = fn(parsed)
            if inspect.isawaitable(value):
                value = await value
        result = {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "detail": repr(value)[:2000]}
    except Exception as exc:  # noqa: BLE001 -- a raising handler is a reportable result, not an exception
        result = {
            "ok": False,
            "ms": int((time.monotonic() - t0) * 1000),
            "detail": f"{type(exc).__name__}: {exc}",
        }

    overlay.record_exercise(ctx.paths, entry, result, records=ctx.records)
    if ctx.image is not None:
        ctx.image.last_seq = entry.seq
    return _ok(f"seq {entry.seq} exercised", exercise=result)


async def _discard(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    value = args["target_or_seq"].strip()
    try:
        if value.isdigit():
            entry = overlay.load_entry(ctx.paths, int(value))
        else:
            found = overlay.latest_entry_for_target(ctx.paths, value)
            if found is None:
                return _error(f"discard refuses {value!r}: no overlay entry for that target")
            entry = found
        overlay.discard(ctx.paths, entry, records=ctx.records)
    except overlay.RedefineRefused as exc:
        return _error(str(exc))
    return _ok(f"discarded seq {entry.seq} ({entry.target})")


async def _commit(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    message = args["message"]
    seq = ctx.image.last_seq if ctx.image is not None else None
    if seq is None:
        return _error("commit refuses: nothing has been redefined/exercised yet this session")
    try:
        entry = overlay.load_entry(ctx.paths, seq)
        result = overlay.commit(ctx.paths, ctx.cfg, entry, message, records=ctx.records)
    except overlay.RedefineRefused as exc:
        return _error(str(exc))
    if not result.ok:
        return _error("commit refused by the replay probe", probe=result.probe)
    return _ok(
        f"committed seq {entry.seq} as generation {result.generation} ({result.commit})",
        generation=result.generation,
        commit=result.commit,
    )


async def _persist_definition(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    target = args["target"]
    entry = overlay.latest_entry_for_target(ctx.paths, target)
    if entry is None or entry.status != "committed":
        return _error(f"persist_definition refuses {target!r}: no committed overlay for it yet")

    try:
        tracked_path, tracked_node, tracked_source = overlay.find_definition(target)
    except overlay.RedefineRefused as exc:
        return _error(str(exc))

    overlay_path = entry.source_path(ctx.paths)
    overlay_source = overlay_path.read_text(encoding="utf-8")
    overlay_tree = ast.parse(overlay_source, filename=str(overlay_path))
    symbol = entry.shadows.symbol
    overlay_node = next(
        (
            n
            for n in ast.walk(overlay_tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == symbol
        ),
        None,
    )
    if overlay_node is None:
        return _error(f"persist_definition: overlay seq {entry.seq} does not define {symbol!r}")

    overlay_segment = ast.get_source_segment(overlay_source, overlay_node)
    if overlay_segment is None:
        return _error("persist_definition: could not extract the overlay's source segment")
    if not overlay_segment.endswith("\n"):
        overlay_segment += "\n"

    lines = tracked_source.splitlines(keepends=True)
    start, end = tracked_node.lineno - 1, tracked_node.end_lineno
    tracked_path.write_text("".join(lines[:start] + [overlay_segment] + lines[end:]), encoding="utf-8")
    return _ok(f"persisted seq {entry.seq}'s definition of {symbol!r} into {tracked_path}")


def _coerce(raw: str) -> Any:
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


async def _set(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    key, raw_value = args["key"], args["value"]
    if "." not in key:
        return _error(f"set refuses {key!r}: expected 'section.field' (e.g. 'claude.effort')")
    section_name, field_name = key.split(".", 1)
    section_obj = getattr(ctx.cfg, section_name, None)
    if section_obj is None or not hasattr(section_obj, field_name):
        return _error(f"set refuses {key!r}: no such config field")
    value = _coerce(raw_value)
    setattr(section_obj, field_name, value)
    return _ok(f"{key} = {value!r} (session-scoped; config.toml is untouched)")


async def _apropos(args: dict[str, Any], ctx: Any) -> dict[str, Any]:
    pattern = args["pattern"]
    try:
        rows = ctx.image.apropos(pattern)
    except re.error as exc:
        return _error(f"apropos: bad pattern {pattern!r}: {exc}")
    return {"content": [{"type": "text", "text": json.dumps(rows, ensure_ascii=False)}]}


def build(ctx: Any) -> list[SdkMcpTool]:
    @tool("status", "Active generation, overlay entries and their statuses, worktree, session, model, "
          "effort, and how many tools are registered.", {})
    async def status(args: dict[str, Any]) -> dict[str, Any]:
        return await _status(args, ctx)

    @tool("describe", "The current effective source of a target ('tool:<ns>.<name>' or "
          "'fn:<module>:<attr>') -- the live overlay if one is applied, otherwise the tracked "
          "definition.", {"target": str})
    async def describe(args: dict[str, Any]) -> dict[str, Any]:
        return await _describe(args, ctx)

    @tool("redefine", "Propose and live-apply a new definition for 'target' from 'source'. For a "
          "'tool:' target, a schema change is always refused -- the wire tool list and schemas are "
          "fixed when the MCP server was created, so only behavior can change without a restart. "
          "Nothing is durable until self.exercise then self.commit.", {"target": str, "source": str})
    async def redefine(args: dict[str, Any]) -> dict[str, Any]:
        return await _redefine(args, ctx)

    @tool("diff", "Unified diff between target's tracked definition and its current live source.",
          {"target": str})
    async def diff(args: dict[str, Any]) -> dict[str, Any]:
        return await _diff(args, ctx)

    @tool("exercise", "Call target's live handler directly with the arguments in 'args_json' (a JSON "
          "object/array/scalar), timing it and recording the outcome -- including a raised exception "
          "-- into the overlay manifest's exercise field.", {"target": str, "args_json": str})
    async def exercise(args: dict[str, Any]) -> dict[str, Any]:
        return await _exercise(args, ctx)

    @tool("discard", "Discard a proposed/exercised overlay entry (by target or by seq number) and "
          "restore the handler it replaced, if it is still applied in this process. A committed "
          "entry cannot be discarded.", {"target_or_seq": str})
    async def discard(args: dict[str, Any]) -> dict[str, Any]:
        return await _discard(args, ctx)

    @tool("commit", "Commit the most recently exercised overlay: re-verifies it via a clean-process "
          "replay probe, then folds it into the private mutations.git history as a new generation.",
          {"message": str})
    async def commit(args: dict[str, Any]) -> dict[str, Any]:
        return await _commit(args, ctx)

    @tool("persist_definition", "Write a committed overlay's definition back into its tracked source "
          "file in the real lictor/ tree, replacing the original. Refuses unless the overlay for "
          "'target' has already been committed.", {"target": str})
    async def persist_definition(args: dict[str, Any]) -> dict[str, Any]:
        return await _persist_definition(args, ctx)

    @tool("set", "Session-scoped config override: 'key' is 'section.field' (e.g. 'claude.effort'), "
          "'value' is parsed as a Python literal if possible. Never written to config.toml.",
          {"key": str, "value": str})
    async def set_tool(args: dict[str, Any]) -> dict[str, Any]:
        return await _set(args, ctx)

    @tool("apropos", "Regex search over registered tool names and lictor.* module attributes, with "
          "a one-line summary of each match.", {"pattern": str})
    async def apropos(args: dict[str, Any]) -> dict[str, Any]:
        return await _apropos(args, ctx)

    return [status, describe, redefine, diff, exercise, discard, commit, persist_definition, set_tool, apropos]
