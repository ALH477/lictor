"""`python -m lictor.probe` -- the replay probe, run in a clean subprocess.

`overlay.commit` launches this before it will ever `git commit` an overlay:
rebuild a tool registry from scratch, apply the named overlay seq numbers
on top of it, and assert the result still looks like a working lictor
image. A failure here is what makes a commit refuse rather than letting a
broken redefinition reach the private git history.

Deliberately does not import the SDK transport, connect to a real `claude`
binary, or touch the operator's real XDG state: every assertion below is
an in-process construction check against a throwaway stub context, never a
live call. The caller (`overlay.run_probe`) is responsible for handing this
process a scrubbed environment; this module scrubs its own `os.environ` too
(via the same rule), so it behaves correctly even invoked directly.
"""

from __future__ import annotations

import argparse
import inspect
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class _StubPaths:
    """Just enough of `config.Paths`' shape (`state`/`data`/`config`) for
    `tools.load`'s `ToolContext` and `options.build_options`'s `State` to be
    constructed, without resolving or touching any real XDG directory."""

    state: Path
    data: Path
    config: Path


def _parse_seqs(raw: str) -> list[int]:
    raw = raw.strip()
    if not raw:
        return []
    return [int(piece) for piece in raw.split(",") if piece.strip()]


def _run_check(checks: list[dict[str, Any]], name: str, fn: Callable[[], str]) -> bool:
    try:
        detail = fn()
    except Exception as exc:  # noqa: BLE001 -- a failing check is data for the report, not a crash
        checks.append({"name": name, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
        return False
    checks.append({"name": name, "ok": True, "detail": str(detail)})
    return True


def run(overlay_dir: Path, seqs: list[int]) -> dict[str, Any]:
    t0 = time.monotonic()
    checks: list[dict[str, Any]] = []

    from . import options as options_mod

    options_mod.scrub_environ()

    from . import config as config_mod
    from . import overlay as overlay_mod
    from . import repl as repl_mod
    from .tools import ToolContext
    from .tools import load as load_tools

    paths = _StubPaths(state=overlay_dir.parent, data=overlay_dir.parent, config=overlay_dir.parent)
    cfg = config_mod.Config()
    ctx = ToolContext(cfg=cfg, state=None, records=None, paths=paths)

    registry_box: dict[str, Any] = {}

    def _load_registry() -> str:
        registry_box["registry"] = load_tools(ctx)
        return f"{len(registry_box['registry'].tools)} tools loaded"

    _run_check(checks, "load_registry", _load_registry)
    registry = registry_box.get("registry")

    if registry is not None and seqs:
        def _apply_overlays() -> str:
            report = overlay_mod.replay(paths, seqs, registry)
            if report.skipped:
                raise AssertionError("skipped: " + "; ".join(s.message for s in report.skipped))
            return f"applied seqs {report.applied}"

        _run_check(checks, "apply_overlays", _apply_overlays)

    def _handlers_are_async() -> str:
        if registry is None:
            raise AssertionError("registry was never built")
        for dotted, sdk_tool in registry.tools.items():
            if not inspect.iscoroutinefunction(sdk_tool.handler):
                raise AssertionError(f"{dotted}: handler is not a coroutine function")
        return f"{len(registry.tools)} handlers are coroutine functions"

    _run_check(checks, "registry_handlers_async", _handlers_are_async)

    def _classify() -> str:
        result = repl_mod.classify("x")
        if not isinstance(result, repl_mod.Submission):
            raise TypeError(f"classify('x') returned {result!r}, not a Submission")
        return "classify('x') -> Submission"

    _run_check(checks, "classify", _classify)

    def _build_options() -> str:
        from .options import State, build_options

        state = State(cwd=overlay_dir, workspace=overlay_dir, active_worktree=None, cid="probe")
        build_options(cfg, state)
        return "build_options constructed a default ClaudeAgentOptions"

    _run_check(checks, "build_options", _build_options)

    def _servers() -> str:
        if registry is None:
            raise AssertionError("registry was never built")
        servers = registry.servers()
        namespaces = registry.namespaces()
        if sorted(servers) != sorted(namespaces):
            raise AssertionError(f"servers() built {sorted(servers)}, expected one per namespace {sorted(namespaces)}")
        return f"{len(servers)} server(s), one per namespace"

    _run_check(checks, "servers", _servers)

    ok = all(c["ok"] for c in checks)
    ms = int((time.monotonic() - t0) * 1000)
    return {"ok": ok, "checks": checks, "ms": ms}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m lictor.probe")
    parser.add_argument("--overlay", required=True, help="overlay directory to replay against")
    parser.add_argument("--seqs", default="", help="comma-separated overlay seq numbers to apply, in order")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        seqs = _parse_seqs(args.seqs)
        result = run(Path(args.overlay), seqs)
    except Exception as exc:  # noqa: BLE001 -- this process must always emit one JSON object, never a traceback
        result = {
            "ok": False,
            "checks": [{"name": "probe", "ok": False, "detail": f"{type(exc).__name__}: {exc}"}],
            "ms": 0,
        }

    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
