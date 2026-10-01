"""probe.py: `python -m lictor.probe` run as a real subprocess -- the last
line of defense before `overlay.commit` ever touches the private git
history. No overlays is a clean pass; an overlay that gets a non-async
handler bound into the registry is caught and named."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from lictor import config as config_mod
from lictor import image as image_mod
from lictor import overlay
from lictor import records as records_mod
from lictor.options import State, scrub_environ
from lictor.tools import ToolContext, load

REPO_ROOT = Path(__file__).resolve().parent.parent

# Constructs an SdkMcpTool directly, bypassing the @tool decorator -- and
# with it, the "must be `async def`" guarantee Python's grammar would
# otherwise give a decorated handler. apply() only checks for exactly one
# @tool object or one async function among the overlay's top-level names;
# it does not itself re-check that an SdkMcpTool's handler is a coroutine
# function. That's exactly the gap the probe's registry_handlers_async
# check exists to catch before a commit ever reaches git.
SYNC_HANDLER_SOURCE = '''
from claude_agent_sdk import SdkMcpTool

def _sync_handler(args):
    return {"content": [{"type": "text", "text": "sync!"}]}

status = SdkMcpTool(name="status", description="broken", input_schema={}, handler=_sync_handler)
'''


def _run_probe(overlay_dir: Path, seqs: str = "", timeout: int = 60) -> dict:
    env = dict(os.environ)
    scrub_environ(env)
    cmd = [sys.executable, "-m", "lictor.probe", "--overlay", str(overlay_dir), "--seqs", seqs]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=timeout, cwd=str(REPO_ROOT), check=False
    )
    assert proc.stdout.strip(), f"probe produced no stdout; stderr={proc.stderr}"
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    return result, proc.returncode


def test_probe_with_no_overlays_is_ok(paths):
    result, returncode = _run_probe(overlay.overlay_dir(paths))
    assert result["ok"] is True, result
    assert returncode == 0
    names = [c["name"] for c in result["checks"]]
    assert "load_registry" in names
    assert "classify" in names
    assert "build_options" in names
    assert "servers" in names


def test_probe_catches_a_non_async_handler_bound_by_an_overlay(paths):
    cfg = config_mod.Config()
    state = State(cwd=paths.state, workspace=paths.state, active_worktree=None, cid="probe-test")
    records = records_mod.Records(paths, "probe-test")
    ctx = ToolContext(cfg=cfg, state=state, records=records, paths=paths)
    registry = load(ctx, namespaces=("self_",))
    img = image_mod.Image(cfg, state, records, paths, registry)
    ctx.image = img

    entry = img.redefine("tool:self.status", SYNC_HANDLER_SOURCE)

    result, returncode = _run_probe(overlay.overlay_dir(paths), seqs=str(entry.seq))

    assert result["ok"] is False, result
    assert returncode == 1
    failing = [c for c in result["checks"] if not c["ok"]]
    assert failing, "expected at least one failing check"
    assert any(c["name"] == "registry_handlers_async" for c in failing), result
