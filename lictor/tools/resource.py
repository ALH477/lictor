"""``resource.*`` tools: ``resource.read(uri)`` over four URI schemes.

    workspace:<relpath>   a file (its text) or a directory (its entries),
                          relative to the active workspace; escaping the
                          workspace root is refused, not clamped
    inference:<trace-id>  the Trace JSON written by lictor/rlm.py
    session:<cid>         a summary of that conversation's JSONL record
    vault:current         the current conversation's vault contents

An unknown scheme gets a result naming the schemes that are supported,
rather than an error dump.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, tool

from .. import records as records_mod
from ..vault import Vault

NAMESPACE = "resource"

SCHEMES = ("workspace", "inference", "session", "vault")


def _text(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)
    return {"content": [{"type": "text", "text": body}], "is_error": is_error}


def _workspace_root(ctx: Any) -> Path:
    state = getattr(ctx, "state", None)
    workspace = getattr(state, "workspace", None)
    if workspace:
        return Path(workspace).resolve()
    return Path(ctx.cfg.workspace.path).resolve()


def _read_workspace(ctx: Any, rest: str) -> dict[str, Any]:
    root = _workspace_root(ctx)
    target = (root / rest).resolve() if rest else root
    try:
        target.relative_to(root)
    except ValueError:
        return _text(f"workspace:{rest} escapes the workspace root ({root}); refused", is_error=True)
    if not target.exists():
        return _text(f"workspace:{rest} does not exist under {root}", is_error=True)
    if target.is_dir():
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return _text({"dir": str(target), "entries": entries})
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return _text(f"workspace:{rest} could not be read: {exc}", is_error=True)
    return _text(text)


def _read_inference(ctx: Any, trace_id: str) -> dict[str, Any]:
    path = ctx.paths.data / "inferences" / f"{trace_id}.json"
    if not path.is_file():
        return _text(f"inference:{trace_id} has no trace at {path}", is_error=True)
    return _text(path.read_text(encoding="utf-8"))


def _read_session(ctx: Any, cid: str) -> dict[str, Any]:
    path = ctx.paths.state / "conversations" / f"{cid}.jsonl"
    if not path.is_file():
        return _text(f"session:{cid} has no conversation record at {path}", is_error=True)
    counts: dict[str, int] = {}
    first_t: str | None = None
    last_t: str | None = None
    meta: dict[str, Any] | None = None
    total = 0
    for obj in records_mod.Records(ctx.paths, cid).iter_lines(cid):
        total += 1
        kind = obj.get("kind", "?")
        counts[kind] = counts.get(kind, 0) + 1
        if first_t is None:
            first_t = obj.get("t")
        last_t = obj.get("t")
        if kind == "meta" and meta is None:
            meta = obj.get("data")
    return _text(
        {"cid": cid, "records": total, "by_kind": counts, "first_t": first_t, "last_t": last_t, "meta": meta}
    )


def _read_vault(ctx: Any, rest: str) -> dict[str, Any]:
    if rest != "current":
        return _text(f"vault:{rest} is not supported; only vault:current is", is_error=True)
    state = getattr(ctx, "state", None)
    cid = getattr(state, "cid", None)
    if not cid:
        return _text("vault:current has no active conversation id in this context", is_error=True)
    vault = Vault(ctx.paths, cid)
    return _text({"cid": cid, "summary": vault.summary(), "items": vault.items()})


def build(ctx: Any) -> list[SdkMcpTool]:
    @tool(
        "read",
        "Read a lictor resource by URI. Schemes: workspace:<relpath> (file or directory, "
        "within the active workspace only), inference:<trace-id> (an rlm Trace), "
        "session:<cid> (a summary of that conversation's record), vault:current (the current "
        "conversation's pending vault items).",
        {"type": "object", "properties": {"uri": {"type": "string"}}, "required": ["uri"]},
    )
    async def _read(args: dict[str, Any]) -> dict[str, Any]:
        uri = args["uri"]
        if ":" not in uri:
            return _text(f"{uri!r} is not a resource URI; supported schemes: {', '.join(SCHEMES)}", is_error=True)
        scheme, rest = uri.split(":", 1)
        if scheme == "workspace":
            return _read_workspace(ctx, rest)
        if scheme == "inference":
            return _read_inference(ctx, rest)
        if scheme == "session":
            return _read_session(ctx, rest)
        if scheme == "vault":
            return _read_vault(ctx, rest)
        return _text(f"unknown scheme {scheme!r}; supported schemes: {', '.join(SCHEMES)}", is_error=True)

    return [_read]
