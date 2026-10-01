"""image.py: the `!` namespace, eval reuse, and redefine()'s seam onto the
overlay journal -- rebinding a tool's handler live, refusing a schema
change, requiring an existing attribute for an `fn:` target, and a
traceback that names the overlay file rather than `<string>`."""

from __future__ import annotations

import traceback

import pytest

from lictor import config as config_mod
from lictor import image as image_mod
from lictor import overlay
from lictor import records as records_mod
from lictor.options import State
from lictor.tools import ToolContext, load

GOOD_STATUS_SOURCE = '''
from claude_agent_sdk import tool

@tool("status", "patched status", {})
async def status(args):
    return {"content": [{"type": "text", "text": "patched!"}]}
'''

SCHEMA_CHANGED_STATUS_SOURCE = '''
from claude_agent_sdk import tool

@tool("status", "a status with a different schema", {"extra": str})
async def status(args):
    return {"content": [{"type": "text", "text": "nope"}]}
'''


def _build_image(paths, cid: str = "test-cid"):
    cfg = config_mod.Config()
    state = State(cwd=paths.state, workspace=paths.state, active_worktree=None, cid=cid)
    records = records_mod.Records(paths, cid)
    ctx = ToolContext(cfg=cfg, state=state, records=records, paths=paths)
    registry = load(ctx, namespaces=("self_",))
    img = image_mod.Image(cfg, state, records, paths, registry)
    ctx.image = img
    return ctx, img, registry


# -- the `!` namespace and eval -------------------------------------------


def test_ns_carries_the_documented_bindings(paths):
    _ctx, img, registry = _build_image(paths)
    ns = img.ns
    for key in ("prompt", "read_file", "state", "cfg", "registry", "image", "lictor", "apropos"):
        assert key in ns
    assert ns["registry"] is registry
    assert ns["image"] is img


def test_eval_execs_statements_and_evaluates_a_trailing_expression(paths):
    _ctx, img, _registry = _build_image(paths)
    assert img.eval("x = 1\nx + 41") == 42


def test_eval_reuses_repl_eval_image(paths):
    from lictor import repl

    _ctx, img, _registry = _build_image(paths)
    # img.eval and repl.eval_image(src, img.ns) must agree -- eval() is a
    # thin wrapper over the shared evaluator, not a reimplementation of it.
    assert img.eval("1 + 1") == repl.eval_image("1 + 1", img.ns)
    assert img.eval("x = 10\nx * 4") == 40


# -- redefine: tool: rebinds live --------------------------------------------


async def test_redefine_rebinds_the_live_handler_and_a_call_reaches_new_code(paths):
    _ctx, img, registry = _build_image(paths)

    entry = img.redefine("tool:self.status", GOOD_STATUS_SOURCE)

    assert entry.status == "proposed"
    assert entry.target == "tool:self.status"
    assert entry.seq == 1

    handler = registry.tools["self.status"].handler
    result = await handler({})
    assert result["content"][0]["text"] == "patched!"


async def test_a_schema_change_is_refused_and_the_original_handler_stays_bound(paths):
    _ctx, img, registry = _build_image(paths)
    original_handler = registry.tools["self.status"].handler

    with pytest.raises(overlay.RedefineRefused, match="schema change needs a restart"):
        img.redefine("tool:self.status", SCHEMA_CHANGED_STATUS_SOURCE)

    assert registry.tools["self.status"].handler is original_handler
    # No manifest should have been written for a refused redefine.
    assert overlay.list_entries(paths) == []


# -- redefine: fn: requires an existing attribute --------------------------


def test_fn_redefine_refuses_a_nonexistent_attribute(paths):
    _ctx, img, _registry = _build_image(paths)
    with pytest.raises(overlay.RedefineRefused, match="requires an existing attribute"):
        img.redefine("fn:lictor.repl:NOT_A_REAL_ATTRIBUTE", "NOT_A_REAL_ATTRIBUTE = 1\n")


def test_fn_redefine_rebinds_a_real_module_attribute_and_discard_restores_it(paths):
    import lictor.repl as repl_mod

    _ctx, img, _registry = _build_image(paths)
    original = repl_mod.ALL_COMMANDS
    entry = None
    try:
        entry = img.redefine("fn:lictor.repl:ALL_COMMANDS", "ALL_COMMANDS = frozenset({'patched'})\n")
        assert repl_mod.ALL_COMMANDS == frozenset({"patched"})
        assert entry.status == "proposed"
    finally:
        if entry is not None:
            overlay.discard(paths, entry)
        assert repl_mod.ALL_COMMANDS == original


def test_fn_redefine_refuses_a_non_lictor_module(paths):
    _ctx, img, _registry = _build_image(paths)
    with pytest.raises(overlay.RedefineRefused, match="non-lictor module"):
        img.redefine("fn:os.path:join", "def join(*a):\n    return ''\n")


# -- traceback names the overlay file, not <string> -------------------------


def test_a_raising_overlay_names_itself_in_the_traceback(paths):
    _ctx, img, _registry = _build_image(paths)

    with pytest.raises(RuntimeError, match="boom") as exc_info:
        img.redefine("tool:self.status", "raise RuntimeError('boom')\n")

    py_files = list(overlay.overlay_dir(paths).glob("*.py"))
    assert len(py_files) == 1
    written_path = str(py_files[0])

    frames = traceback.extract_tb(exc_info.value.__traceback__)
    assert any(frame.filename == written_path for frame in frames)

    # A refused/failed redefine never gets a manifest -- the seq is burned,
    # but there is nothing to list.
    assert overlay.list_entries(paths) == []


# -- apropos ----------------------------------------------------------------


def test_apropos_finds_registered_tools_by_regex(paths):
    _ctx, img, _registry = _build_image(paths)
    rows = img.apropos(r"^self\.status$")
    assert any(row["name"] == "self.status" for row in rows)


def test_apropos_finds_lictor_module_attributes(paths):
    _ctx, img, _registry = _build_image(paths)
    rows = img.apropos(r"lictor\.repl\.classify$")
    assert any(row["name"] == "lictor.repl.classify" for row in rows)
