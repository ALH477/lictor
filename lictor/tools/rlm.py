"""``rlm.*`` tools: bounded recursive inference, over ``ctx.budget`` /
``ctx.cfg.rlm``. See ``lictor/rlm.py`` for what a call actually does.

Every one of these tools spends turns from the session's shared
:class:`lictor.rlm.Budget` and runs a brand-new, tool-less, memory-less
``claude_agent_sdk.query()`` against only the context explicitly passed
in -- never the running conversation, never lictor's own files unless
handed to it as context. Descriptions below say so, since a model
deciding whether to call one of these needs to know it is not "asking
itself a question" but spawning a separate, disposable sub-call that
costs budget.
"""

from __future__ import annotations

import json
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from .. import rlm as rlm_mod

NAMESPACE = "rlm"

_NO_BUDGET = {
    "content": [{"type": "text", "text": "rlm.* is unavailable: no turn Budget is wired up in this session."}],
    "is_error": True,
}


def _text(payload: Any) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": body}]}


def _refused(exc: Exception) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}


def _trace_row(trace: Any) -> dict[str, Any]:
    return {
        "id": trace.id,
        "result": trace.result,
        "structured_output": trace.structured_output,
        "error": trace.error,
        "budget": trace.budget,
    }


def build(ctx: Any) -> list[SdkMcpTool]:
    @tool(
        "infer",
        "Run one fresh, tool-less, memory-less inference call against explicit context you "
        "provide. Costs `max_turns` from the shared rlm turn budget. Give an output JSON "
        "schema to get a structured answer back.",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "context": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "file paths to inline into the prompt, each hashed and size-capped",
                },
                "schema": {"type": "object", "description": "JSON schema the reply must conform to"},
                "model": {"type": "string"},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
                "max_turns": {"type": "integer"},
            },
            "required": ["prompt"],
        },
    )
    async def _infer(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.budget is None:
            return _NO_BUDGET
        try:
            trace = await rlm_mod.infer(
                ctx,
                args["prompt"],
                context=args.get("context") or (),
                schema=args.get("schema"),
                model=args.get("model"),
                effort=args.get("effort", "low"),
                max_turns=args.get("max_turns", 1),
            )
        except (rlm_mod.BudgetExhausted, rlm_mod.ContextTooLarge) as exc:
            return _refused(exc)
        return _text(_trace_row(trace))

    @tool(
        "map",
        "Run rlm.infer once per item in `items`, up to `concurrency` at a time, each costing "
        "its own turns from the shared budget. One item failing does not fail the others.",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "items": {"type": "array", "items": {"type": "string"}},
                "concurrency": {"type": "integer"},
                "context": {"type": "array", "items": {"type": "string"}},
                "schema": {"type": "object"},
                "model": {"type": "string"},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
                "max_turns": {"type": "integer"},
            },
            "required": ["prompt", "items"],
        },
    )
    async def _map(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.budget is None:
            return _NO_BUDGET
        try:
            traces = await rlm_mod.map_(
                ctx,
                args["prompt"],
                args["items"],
                concurrency=args.get("concurrency"),
                context=args.get("context") or (),
                schema=args.get("schema"),
                model=args.get("model"),
                effort=args.get("effort", "low"),
                max_turns=args.get("max_turns", 1),
            )
        except (rlm_mod.BudgetExhausted, rlm_mod.ContextTooLarge) as exc:
            return _refused(exc)
        return _text([_trace_row(t) for t in traces])

    @tool(
        "complete",
        "rlm.infer with max_turns=1 and no output schema: the simplest one-shot, tool-less "
        "question against explicit context. Costs one turn from the shared budget.",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "context": {"type": "array", "items": {"type": "string"}},
                "model": {"type": "string"},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh", "max"]},
            },
            "required": ["prompt"],
        },
    )
    async def _complete(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.budget is None:
            return _NO_BUDGET
        try:
            trace = await rlm_mod.infer(
                ctx,
                args["prompt"],
                context=args.get("context") or (),
                schema=None,
                model=args.get("model"),
                effort=args.get("effort", "low"),
                max_turns=1,
            )
        except (rlm_mod.BudgetExhausted, rlm_mod.ContextTooLarge) as exc:
            return _refused(exc)
        return _text(_trace_row(trace))

    return [_infer, _map, _complete]
