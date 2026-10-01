"""``py.*`` tools: isolated Python worker REPLs, over ``ctx.workers``.

Wire names have no dots (``py.eval`` is ``mcp__py__eval``); see
``lictor/tools/__init__.py`` for the registry contract this module
implements (``NAMESPACE`` + ``build(ctx)``).
"""

from __future__ import annotations

from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

NAMESPACE = "py"

_NO_WORKERS = {
    "content": [
        {
            "type": "text",
            "text": "py.* is unavailable: no WorkerManager is wired up in this session.",
        }
    ],
    "is_error": True,
}


def _text(payload: Any) -> dict[str, Any]:
    import json

    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    is_error = isinstance(payload, dict) and payload.get("ok") is False
    return {"content": [{"type": "text", "text": body}], "is_error": is_error}


def build(ctx: Any) -> list[SdkMcpTool]:
    @tool(
        "eval",
        "Evaluate Python code in a persistent, isolated worker process started by py.start "
        "(or auto-started under the given name). State persists across calls. A call that "
        "runs past `timeout` seconds is killed and the worker restarted on the next call -- "
        "this never touches lictor's own process or code.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "worker name"},
                "code": {"type": "string", "description": "Python source; the trailing bare expression, if any, is evaluated and reported"},
                "timeout": {"type": "number", "description": "seconds before the worker is killed (default 30)"},
            },
            "required": ["name", "code"],
        },
    )
    async def _eval(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.workers is None:
            return _NO_WORKERS
        result = await ctx.workers.eval(args["name"], args["code"], args.get("timeout"))
        return _text(result)

    @tool(
        "start",
        "Start a new py.* worker under `name`, optionally in `cwd`. Error if that name is "
        "already running.",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "cwd": {"type": "string", "description": "working directory for the worker (optional)"},
            },
            "required": ["name"],
        },
    )
    async def _start(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.workers is None:
            return _NO_WORKERS
        try:
            worker = await ctx.workers.start(args["name"], args.get("cwd"))
        except ValueError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
        return _text(f"worker {args['name']!r} started, pid {worker.proc.pid}")

    @tool(
        "stop",
        "Kill a py.* worker by name and forget it.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    async def _stop(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.workers is None:
            return _NO_WORKERS
        try:
            await ctx.workers.stop(args["name"])
        except KeyError:
            return {"content": [{"type": "text", "text": f"no worker named {args['name']!r}"}], "is_error": True}
        return _text(f"worker {args['name']!r} stopped")

    @tool(
        "repls",
        "List every py.* worker: name, pid, alive, last used, eval count.",
        {"type": "object", "properties": {}},
    )
    async def _repls(_args: dict[str, Any]) -> dict[str, Any]:
        if ctx.workers is None:
            return _NO_WORKERS
        return _text(ctx.workers.repls())

    @tool(
        "reset",
        "Clear a py.* worker's namespace without restarting the process.",
        {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    )
    async def _reset(args: dict[str, Any]) -> dict[str, Any]:
        if ctx.workers is None:
            return _NO_WORKERS
        try:
            result = await ctx.workers.reset(args["name"])
        except KeyError:
            return {
                "content": [{"type": "text", "text": f"no running worker named {args['name']!r}"}],
                "is_error": True,
            }
        return _text(result)

    return [_eval, _start, _stop, _repls, _reset]
