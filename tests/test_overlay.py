"""overlay.py: the journal's own contract -- manifest shape, ordered
replay, the skip when a tracked definition has moved, commit's exercised
precondition, and generations/rollback/discard."""

from __future__ import annotations

import sys
import types

import pytest

from lictor import config as config_mod
from lictor import image as image_mod
from lictor import overlay
from lictor import records as records_mod
from lictor.options import State
from lictor.tools import ToolContext, load

GOOD_APROPOS_SOURCE = '''
from claude_agent_sdk import tool

@tool("apropos", "patched apropos", {"pattern": str})
async def apropos(args):
    return {"content": [{"type": "text", "text": "[]"}]}
'''

GOOD_STATUS_SOURCE = '''
from claude_agent_sdk import tool

@tool("status", "patched status", {})
async def status(args):
    return {"content": [{"type": "text", "text": "patched!"}]}
'''


def _build_image(paths, cid: str = "test-cid"):
    cfg = config_mod.Config()
    state = State(cwd=paths.state, workspace=paths.state, active_worktree=None, cid=cid)
    records = records_mod.Records(paths, cid)
    ctx = ToolContext(cfg=cfg, state=state, records=records, paths=paths)
    registry = load(ctx, namespaces=("self_",))
    img = image_mod.Image(cfg, state, records, paths, registry)
    ctx.image = img
    return cfg, img, registry


def _install_fixture_module(tmp_path, content: str, name: str = "lictor._test_overlay_fixture"):
    """A throwaway `lictor.*`-named module backed by a file under `tmp_path`
    -- never a real file in this repo -- so the `fn:` scheme's "tracked
    definition moved" test can move it without touching anything owned by
    another agent (or by us, outside this milestone's own test files)."""
    path = tmp_path / f"{name.rsplit('.', 1)[-1]}_src.py"
    path.write_text(content, encoding="utf-8")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    exec(compile(content, str(path), "exec"), module.__dict__)  # noqa: S102
    sys.modules[name] = module
    return module, path


def _remove_fixture_module(name: str = "lictor._test_overlay_fixture") -> None:
    sys.modules.pop(name, None)


# -- manifest shape -------------------------------------------------------


def test_manifest_shape_has_both_hashes(paths):
    _cfg, img, _registry = _build_image(paths)
    entry = img.redefine("tool:self.apropos", GOOD_APROPOS_SOURCE)

    d = entry.to_dict()
    assert set(d) == {
        "seq", "target", "created", "cid", "lictor", "sdk", "claude", "shadows", "status", "exercise", "commit",
    }
    assert d["seq"] == 1
    assert d["target"] == "tool:self.apropos"
    assert d["status"] == "proposed"
    assert d["exercise"] is None
    assert d["commit"] is None

    shadow = d["shadows"]
    assert set(shadow) == {"path", "file_sha256", "symbol", "symbol_sha256"}
    assert shadow["symbol"] == "apropos"
    assert len(shadow["file_sha256"]) == 64
    assert len(shadow["symbol_sha256"]) == 64

    # The manifest on disk round-trips through from_dict.
    reloaded = overlay.load_entry(paths, 1)
    assert reloaded.to_dict() == d


# -- replay: ordered application --------------------------------------------


async def test_replay_applies_committed_overlays_in_order(paths):
    _cfg, img, _registry = _build_image(paths)
    entry1 = img.redefine("tool:self.status", GOOD_STATUS_SOURCE)
    entry2 = img.redefine("tool:self.apropos", GOOD_APROPOS_SOURCE)

    # A fresh registry, as a new process would build at startup.
    cfg2 = config_mod.Config()
    state2 = State(cwd=paths.state, workspace=paths.state, active_worktree=None, cid="fresh-cid")
    ctx2 = ToolContext(cfg=cfg2, state=state2, records=None, paths=paths)
    registry2 = load(ctx2, namespaces=("self_",))

    report = overlay.replay(paths, [entry1.seq, entry2.seq], registry2)

    assert report.applied == [entry1.seq, entry2.seq]
    assert report.skipped == []

    status_result = await registry2.tools["self.status"].handler({})
    apropos_result = await registry2.tools["self.apropos"].handler({"pattern": "x"})
    assert status_result["content"][0]["text"] == "patched!"
    assert apropos_result["content"][0]["text"] == "[]"


# -- replay: the skip case --------------------------------------------------


def test_replay_skips_when_the_tracked_definition_has_moved(paths, tmp_path):
    _module_a, path_a = _install_fixture_module(
        tmp_path, "VALUE = 'original'\n", name="lictor._test_overlay_fixture_a"
    )
    _module_b, _path_b = _install_fixture_module(
        tmp_path, "OTHER = 'original'\n", name="lictor._test_overlay_fixture_b"
    )
    try:
        cfg = config_mod.Config()
        moved_entry = overlay.redefine(
            paths, cfg, "cid", None, "fn:lictor._test_overlay_fixture_a:VALUE", "VALUE = 'patched'\n"
        )
        # A second, unrelated entry on a *different* tracked definition,
        # which should still apply even though the first one is skipped.
        stable_entry = overlay.redefine(
            paths, cfg, "cid", None, "fn:lictor._test_overlay_fixture_b:OTHER", "OTHER = 'patched'\n"
        )

        # Now move the first fixture's tracked definition out from under it.
        path_a.write_text("VALUE = 'moved!!'\n", encoding="utf-8")

        report = overlay.replay(paths, [moved_entry.seq, stable_entry.seq], None)

        assert len(report.skipped) == 1
        assert report.skipped[0].seq == moved_entry.seq
        assert report.skipped[0].message == "tracked definition moved since publication"
        assert report.applied == [stable_entry.seq]
        assert sys.modules["lictor._test_overlay_fixture_b"].OTHER == "patched"
    finally:
        _remove_fixture_module("lictor._test_overlay_fixture_a")
        _remove_fixture_module("lictor._test_overlay_fixture_b")


# -- commit's exercised precondition ----------------------------------------


def test_commit_refuses_a_non_exercised_entry(paths):
    cfg, img, _registry = _build_image(paths)
    entry = img.redefine("tool:self.status", GOOD_STATUS_SOURCE)

    with pytest.raises(overlay.RedefineRefused, match="not 'exercised'"):
        overlay.commit(paths, cfg, entry, "should refuse")


# -- generations, rollback, discard -----------------------------------------


def test_commit_then_generations_active_generation_and_rollback(paths):
    cfg, img, _registry = _build_image(paths)
    entry = img.redefine("tool:self.status", GOOD_STATUS_SOURCE)
    overlay.record_exercise(paths, entry, {"ok": True, "ms": 1, "detail": "fine"})

    result = overlay.commit(paths, cfg, entry, "patch self.status")
    assert result.ok, result.probe
    assert result.generation == 1

    gens = overlay.generations(paths)
    assert [g["gen"] for g in gens] == [1]
    assert gens[0]["overlay_seqs"] == [entry.seq]
    assert gens[0]["mutations_commit"] == result.commit

    assert overlay.active_generation(paths) == 1

    overlay.rollback(paths, 0)
    assert overlay.active_generation(paths) == 0

    with pytest.raises(overlay.RedefineRefused, match="unknown"):
        overlay.rollback(paths, 99)


def test_discard_restores_the_handler_in_this_process(paths):
    _cfg, img, registry = _build_image(paths)
    original_handler = registry.tools["self.status"].handler

    entry = img.redefine("tool:self.status", GOOD_STATUS_SOURCE)
    assert registry.tools["self.status"].handler is not original_handler

    overlay.discard(paths, entry)
    assert registry.tools["self.status"].handler is original_handler

    reloaded = overlay.load_entry(paths, entry.seq)
    assert reloaded.status == "discarded"


def test_discard_refuses_an_already_committed_entry(paths):
    cfg, img, _registry = _build_image(paths)
    entry = img.redefine("tool:self.status", GOOD_STATUS_SOURCE)
    overlay.record_exercise(paths, entry, {"ok": True, "ms": 1, "detail": "fine"})
    result = overlay.commit(paths, cfg, entry, "patch self.status")
    assert result.ok, result.probe

    with pytest.raises(overlay.RedefineRefused, match="already committed"):
        overlay.discard(paths, entry)
