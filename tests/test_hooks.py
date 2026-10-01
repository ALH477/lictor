"""Offline tests for lictor/hooks.py. Hook callbacks are called directly
with hand-built `(input, tool_use_id, hook_context)` tuples -- the shape
the SDK's HookMatcher expects -- against the real `Config`/`State`/
`Records` plumbing on `tmp_path` (via the `paths` fixture).
"""

from __future__ import annotations

from pathlib import Path

from lictor import options as options_mod
from lictor.hooks import build_hooks
from lictor.records import Records
from lictor.tools import ToolContext


def _make_ctx(paths, *, workspace: Path, active_worktree=None) -> ToolContext:
    state = options_mod.State(
        cwd=workspace,
        workspace=workspace,
        active_worktree=active_worktree,
        cid="t-hooks",
    )
    records = Records(paths=paths, cid="t-hooks")
    return ToolContext(cfg=None, state=state, records=records, paths=paths)


def _matcher_hooks(hooks: dict, event: str, index: int):
    return hooks[event][index].hooks[0]


# ---------------------------------------------------------------------------
# fence_edits
# ---------------------------------------------------------------------------


async def test_fence_denies_path_outside_worktree(paths, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = _make_ctx(paths, workspace=tmp_path, active_worktree=worktree)
    hooks = build_hooks(ctx)
    fence_edits = _matcher_hooks(hooks, "PreToolUse", 0)

    outside = tmp_path / "outside.txt"
    result = await fence_edits({"tool_input": {"file_path": str(outside)}}, "id1", None)

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert str(worktree) in reason
    assert "exs.worktree" in reason


async def test_fence_allows_path_inside_worktree(paths, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = _make_ctx(paths, workspace=tmp_path, active_worktree=worktree)
    hooks = build_hooks(ctx)
    fence_edits = _matcher_hooks(hooks, "PreToolUse", 0)

    inside = worktree / "a.txt"
    result = await fence_edits({"tool_input": {"file_path": str(inside)}}, "id2", None)

    assert result == {}


async def test_fence_allows_state_dir_regardless_of_worktree(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path, active_worktree=None)
    hooks = build_hooks(ctx)
    fence_edits = _matcher_hooks(hooks, "PreToolUse", 0)

    journal_path = Path(ctx.paths.state) / "journal.txt"
    result = await fence_edits({"tool_input": {"file_path": str(journal_path)}}, "id3", None)

    assert result == {}


async def test_fence_denies_with_no_active_worktree(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path, active_worktree=None)
    hooks = build_hooks(ctx)
    fence_edits = _matcher_hooks(hooks, "PreToolUse", 0)

    somewhere = tmp_path / "README.md"
    result = await fence_edits({"tool_input": {"file_path": str(somewhere)}}, "id4", None)

    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "none" in result["hookSpecificOutput"]["permissionDecisionReason"]


# ---------------------------------------------------------------------------
# guard_bash
# ---------------------------------------------------------------------------


async def test_guard_bash_denies_no_verify(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    guard_bash = _matcher_hooks(hooks, "PreToolUse", 1)

    result = await guard_bash({"tool_input": {"command": "git commit --no-verify -m x"}}, "id5", None)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--no-verify" in reason


async def test_guard_bash_denies_git_push(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    guard_bash = _matcher_hooks(hooks, "PreToolUse", 1)

    result = await guard_bash({"tool_input": {"command": "git push origin main"}}, "id6", None)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "git push" in reason


async def test_guard_bash_denies_rm_rf_root(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    guard_bash = _matcher_hooks(hooks, "PreToolUse", 1)

    result = await guard_bash({"tool_input": {"command": "rm -rf / "}}, "id7", None)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "rm -rf /" in reason


async def test_guard_bash_denies_worktree_remove_force(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    guard_bash = _matcher_hooks(hooks, "PreToolUse", 1)

    result = await guard_bash(
        {"tool_input": {"command": "git worktree remove ../x --force"}}, "id8", None
    )
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "worktree remove --force" in reason


async def test_guard_bash_denies_commit_outside_worktree(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path, active_worktree=None)
    hooks = build_hooks(ctx)
    guard_bash = _matcher_hooks(hooks, "PreToolUse", 1)

    result = await guard_bash({"tool_input": {"command": "git commit -m hello"}}, "id9", None)
    reason = result["hookSpecificOutput"]["permissionDecisionReason"]
    assert "shared with" in reason


async def test_guard_bash_allows_commit_inside_worktree(paths, tmp_path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    ctx = _make_ctx(paths, workspace=tmp_path, active_worktree=worktree)
    hooks = build_hooks(ctx)
    guard_bash = _matcher_hooks(hooks, "PreToolUse", 1)

    result = await guard_bash({"tool_input": {"command": "git commit -m hello"}}, "id10", None)
    assert result == {}


async def test_guard_bash_allows_ordinary_command(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    guard_bash = _matcher_hooks(hooks, "PreToolUse", 1)

    result = await guard_bash({"tool_input": {"command": "ls -la"}}, "id11", None)
    assert result == {}


# ---------------------------------------------------------------------------
# record_tool_result
# ---------------------------------------------------------------------------


async def test_record_tool_result_records_length_only(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    record = _matcher_hooks(hooks, "PostToolUse", 0)

    secret_output = "this is secret tool output, 31 chars"
    result = await record(
        {"tool_name": "Bash", "tool_response": secret_output}, "id12", None
    )

    assert result == {}
    rows = list(ctx.records.iter_lines())
    last = rows[-1]
    assert last["kind"] == "hook_tool_result"
    assert last["data"]["tool_name"] == "Bash"
    assert last["data"]["tool_use_id"] == "id12"
    assert last["data"]["length"] == len(secret_output)
    assert secret_output not in str(last["data"])


# ---------------------------------------------------------------------------
# a raising callback body returns {} and records an error
# ---------------------------------------------------------------------------


async def test_fence_edits_never_raises(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    fence_edits = _matcher_hooks(hooks, "PreToolUse", 0)

    result = await fence_edits(None, "id13", None)  # inp.get(...) on None raises AttributeError

    assert result == {}
    rows = list(ctx.records.iter_lines())
    last = rows[-1]
    assert last["kind"] == "error"
    assert last["data"]["where"] == "fence_edits"


async def test_guard_bash_never_raises(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    guard_bash = _matcher_hooks(hooks, "PreToolUse", 1)

    result = await guard_bash(None, "id14", None)

    assert result == {}
    rows = list(ctx.records.iter_lines())
    last = rows[-1]
    assert last["kind"] == "error"
    assert last["data"]["where"] == "guard_bash"


async def test_record_tool_result_never_raises(paths, tmp_path):
    ctx = _make_ctx(paths, workspace=tmp_path)
    hooks = build_hooks(ctx)
    record = _matcher_hooks(hooks, "PostToolUse", 0)

    result = await record(None, "id15", None)

    assert result == {}
    rows = list(ctx.records.iter_lines())
    last = rows[-1]
    assert last["kind"] == "error"
    assert last["data"]["where"] == "record_tool_result"
