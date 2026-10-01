"""lictor launcher: argument parsing, environment scrub, boot banner.

The environment scrub (``options.scrub_environ``) must run before
``claude_agent_sdk`` -- or any lictor module that imports it -- is ever
imported, since the SDK's subprocess transport reads ``os.environ`` at
construction time. Every import that reaches the SDK is therefore deferred
until after the scrub runs, inside ``main()``.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
import shutil
import subprocess
import sys
import uuid

from . import __version__


def _claude_version_text(cli_path: str | None) -> str:
    if cli_path is None:
        return "not found"
    try:
        out = subprocess.run([cli_path, "--version"], capture_output=True, text=True, check=False)
        text = (out.stdout or out.stderr).strip()
        return text or "not found"
    except OSError:
        return "not found"


def _sdk_version() -> str:
    try:
        return importlib.metadata.version("claude-agent-sdk")
    except importlib.metadata.PackageNotFoundError:
        return "not found"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lictor")
    parser.add_argument("--version", action="store_true", help="print lictor/sdk/claude versions and exit")
    parser.add_argument("--once", metavar="TEXT", help="run one turn non-interactively, then exit")
    parser.add_argument("--resume", metavar="CID", help="resume an existing conversation id")
    parser.add_argument("--workspace", metavar="PATH", help="override the configured workspace path")
    parser.add_argument("--model", help="override the configured model")
    parser.add_argument("--effort", help="override the configured effort level")
    parser.add_argument("--mode", help="override the configured permission mode")
    parser.add_argument(
        "--force",
        action="store_true",
        help="start even though CLAUDE_CODE_MESSAGING_SOCKET indicates a nested Claude Code session",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = _build_parser().parse_args(argv)

    if args.version:
        print(f"lictor: {__version__}")
        print(f"claude-agent-sdk: {_sdk_version()}")
        cli_path = os.environ.get("LICTOR_CLAUDE") or shutil.which("claude")
        print(f"claude: {_claude_version_text(cli_path)}")
        return 0

    # Capture this before the scrub removes it -- scrub_environ() deletes
    # every CLAUDE_CODE_* key, this one included.
    had_messaging_socket = bool(os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET"))

    from .options import scrub_environ

    scrub_environ()

    if had_messaging_socket and not args.force:
        print(
            "lictor: refusing to start inside a running Claude Code session "
            "(CLAUDE_CODE_MESSAGING_SOCKET was set) -- an SDK-driven agent "
            "nested inside one is unsupported. Pass --force to override.",
            file=sys.stderr,
        )
        return 1

    from pathlib import Path

    from . import config as config_mod
    from . import records as records_mod
    from .app import App
    from .brain import Brain
    from .options import State
    from .permissions import Approvals
    from .render import Renderer

    paths = config_mod.Paths.resolve()
    paths.ensure()

    cfg = config_mod.Config.load(paths, workspace_override=args.workspace)
    cfg.apply_cli_overrides(model=args.model, effort=args.effort, mode=args.mode)

    workspace = Path(cfg.workspace.path).resolve()

    if args.resume:
        cid = args.resume
        resume_target: str | None = args.resume
    else:
        cid = str(uuid.uuid4())
        resume_target = None

    records = records_mod.Records(paths, cid)

    cli_path = cfg.resolve_cli_path()
    sdk_version = _sdk_version()
    claude_version = _claude_version_text(cli_path)

    records.append(
        "meta",
        {
            "lictor": __version__,
            "sdk": sdk_version,
            "claude": claude_version,
            "cwd": str(workspace),
            "worktree": None,
            "model": cfg.claude.model or None,
            "effort": cfg.claude.effort,
            "generation": 0,
            "recovery": False,
        },
    )

    state = State(
        cwd=workspace,
        workspace=workspace,
        active_worktree=None,
        cid=cid,
        claude_session=resume_target,
        model=cfg.claude.model or None,
        effort=cfg.claude.effort,
        mode=cfg.claude.permission_mode,
    )

    approvals = Approvals()
    renderer = Renderer()

    from lictor import hooks as hooks_mod
    from lictor.tools import ToolContext
    from lictor.tools import load as load_tools

    tool_ctx = ToolContext(cfg=cfg, state=state, records=records, paths=paths)
    registry = load_tools(tool_ctx)
    session_hooks = hooks_mod.build_hooks(tool_ctx)

    brain = Brain(
        cfg, state, records, approvals, renderer,
        registry=registry, hooks=session_hooks,
    )
    app = App(cfg, state, records, approvals, brain, renderer)
    tool_ctx.app = app

    print(
        f"lictor {__version__} · claude {claude_version} · sdk {sdk_version} "
        f"· workspace {workspace} · cid {cid}"
    )
    print(f"· tools {len(registry.tools)} in {len(registry.namespaces())} namespaces")

    if args.once is not None:
        return app.run_once(args.once)

    return app.run()


if __name__ == "__main__":
    raise SystemExit(main())
