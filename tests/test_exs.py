"""Offline tests for lictor/tools/exs.py: no real `claude`, no real nix
builds. `_run` is exercised directly with small real subprocesses
(`/bin/sh`, `sleep`) run with `nix=False`, plus one case that spies on
the real `asyncio.create_subprocess_exec` just to capture the pid for the
timeout/process-group assertion. The tool handlers are exercised through
`build(ctx)` so the test proves the production wiring, using the real
`Config`/`State`/`Records` against `tmp_path` (the `paths` fixture from
conftest.py already points every XDG dir there).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from lictor import options as options_mod
from lictor.config import Config
from lictor.records import Records
from lictor.tools import ToolContext
from lictor.tools import exs as exs_mod


def _make_ctx(paths, workspace: Path) -> ToolContext:
    cfg = Config()
    state = options_mod.State(
        cwd=workspace,
        workspace=workspace,
        active_worktree=None,
        cid="t-exs",
    )
    records = Records(paths=paths, cid="t-exs")
    return ToolContext(cfg=cfg, state=state, records=records, paths=paths)


def _tool_by_name(tools, name: str):
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(name)


# ---------------------------------------------------------------------------
# _run itself
# ---------------------------------------------------------------------------


async def test_run_reports_nonzero_exit_and_writes_artifact(paths, tmp_path):
    ctx = _make_ctx(paths, tmp_path)
    res = await exs_mod._run(ctx, ["/bin/sh", "-c", "echo hi; exit 3"], tool="t1", nix=False)

    assert res["exit"] == 3
    assert res["timed_out"] is False
    assert res["argv"] == ["/bin/sh", "-c", "echo hi; exit 3"]

    artifact = Path(res["artifact"])
    assert artifact.is_file()
    assert artifact.read_text().strip() == "hi"
    assert "hi" in res["tail"]

    lines = list(ctx.records.iter_lines())
    last = lines[-1]
    assert last["kind"] == "exs_run"
    assert last["data"]["tool"] == "t1"
    assert last["data"]["exit"] == 3
    assert last["data"]["timed_out"] is False
    assert last["data"]["artifact"] == str(artifact)
    assert last["data"]["bytes"] == len(b"hi\n")


async def test_run_nix_prefix_is_prepended(paths, tmp_path, monkeypatch):
    ctx = _make_ctx(paths, tmp_path)

    class _FakeStdout:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._served = False

        async def read(self, n: int = -1) -> bytes:
            if self._served:
                return b""
            self._served = True
            return self._data

    class _FakeProc:
        def __init__(self) -> None:
            self.pid = 999999
            self.returncode = 0
            self.stdout = _FakeStdout(b"ok\n")

        async def wait(self) -> int:
            return 0

    captured: dict[str, object] = {}

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured["argv"] = args
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    res = await exs_mod._run(ctx, ["make", "all"], tool="t2", nix=True)

    assert captured["argv"] == ("nix", "develop", "-c", "make", "all")
    assert res["argv"] == ["nix", "develop", "-c", "make", "all"]
    assert res["exit"] == 0


async def test_run_timeout_kills_the_process_group(paths, tmp_path, monkeypatch):
    ctx = _make_ctx(paths, tmp_path)
    captured: dict[str, object] = {}
    real_create = asyncio.create_subprocess_exec

    async def spying_create(*args, **kwargs):
        proc = await real_create(*args, **kwargs)
        captured["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spying_create)

    res = await exs_mod._run(ctx, ["sleep", "5"], tool="t3", timeout=1, nix=False)

    assert res["timed_out"] is True
    assert "timeout after 1s" in res["tail"] or res["timed_out"]

    proc = captured["proc"]
    assert proc.returncode is not None  # reaped: the kill was observed

    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)


# ---------------------------------------------------------------------------
# spec()
# ---------------------------------------------------------------------------

_FIXTURE_SPEC = """\
# 1. Intro

intro text

# 8. Surface syntax and diagnostics

## 8.1 Source

one

## 8.2 Identifiers

two

## 8.3 Diagnostics

three
threeb

## 8.4 Tokens

four

# 13. Error registry

| code | meaning |
|---|---|
| `EXS-E0101` | source not UTF-8, or BOM present |
| `EXS-E0103` | bidi or invisible control character in source |

# 14. Conformance suite

nope
"""


def _write_fixture_spec(workspace: Path) -> None:
    spec_dir = workspace / "docs" / "spec"
    spec_dir.mkdir(parents=True, exist_ok=True)
    (spec_dir / "exsecutor-spec-v0.4.md").write_text(_FIXTURE_SPEC, encoding="utf-8")


async def test_spec_slices_top_level_section(paths, tmp_path):
    _write_fixture_spec(tmp_path)
    ctx = _make_ctx(paths, tmp_path)
    spec_tool = _tool_by_name(exs_mod.build(ctx), "spec")

    result = await spec_tool.handler({"section": "13"})
    text = result["content"][0]["text"]
    assert "EXS-E0101" in text
    assert "EXS-E0103" in text
    assert "14. Conformance" not in text
    assert not result.get("isError")


async def test_spec_slices_subsection(paths, tmp_path):
    _write_fixture_spec(tmp_path)
    ctx = _make_ctx(paths, tmp_path)
    spec_tool = _tool_by_name(exs_mod.build(ctx), "spec")

    result = await spec_tool.handler({"section": "§8.3"})
    text = result["content"][0]["text"]
    assert "three" in text
    assert "threeb" in text
    assert "8.4 Tokens" not in text
    assert "four" not in text


async def test_spec_reports_absent_section(paths, tmp_path):
    _write_fixture_spec(tmp_path)
    ctx = _make_ctx(paths, tmp_path)
    spec_tool = _tool_by_name(exs_mod.build(ctx), "spec")

    result = await spec_tool.handler({"section": "99"})
    assert result.get("isError")
    text = result["content"][0]["text"]
    assert "no section" in text
    assert "1. Intro" in text


# ---------------------------------------------------------------------------
# codes()
# ---------------------------------------------------------------------------


def _write_fixture_codes_inc(workspace: Path, *, include_e0101: bool) -> None:
    inc_dir = workspace / "compiler" / "x86_64" / "diag"
    inc_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    if include_e0101:
        lines.append("\tdb\t'EXS-E0101'")
    lines.append("; EXS-E0103 present here")
    (inc_dir / "codes.inc").write_text("\n".join(lines), encoding="utf-8")


async def test_codes_reports_presence_and_drift(paths, tmp_path):
    _write_fixture_spec(tmp_path)
    _write_fixture_codes_inc(tmp_path, include_e0101=False)
    ctx = _make_ctx(paths, tmp_path)
    codes_tool = _tool_by_name(exs_mod.build(ctx), "codes")

    result = await codes_tool.handler({"query": "E0101"})
    text = result["content"][0]["text"]
    assert "EXS-E0101" in text
    assert "DRIFT" in text

    result2 = await codes_tool.handler({"query": "E0103"})
    text2 = result2["content"][0]["text"]
    assert "EXS-E0103" in text2
    assert "present in codes.inc" in text2


async def test_codes_reports_no_match(paths, tmp_path):
    _write_fixture_spec(tmp_path)
    _write_fixture_codes_inc(tmp_path, include_e0101=True)
    ctx = _make_ctx(paths, tmp_path)
    codes_tool = _tool_by_name(exs_mod.build(ctx), "codes")

    result = await codes_tool.handler({"query": "nothing-matches-this"})
    assert result.get("isError")


# ---------------------------------------------------------------------------
# worktree()
# ---------------------------------------------------------------------------


async def test_worktree_rejects_bad_name(paths, tmp_path):
    ctx = _make_ctx(paths, tmp_path)
    wt_tool = _tool_by_name(exs_mod.build(ctx), "worktree")

    result = await wt_tool.handler({"op": "new", "name": "BAD_NAME!"})
    assert result.get("isError")
    assert ctx.state.active_worktree is None


async def test_worktree_refuses_force(paths, tmp_path):
    ctx = _make_ctx(paths, tmp_path)
    wt_tool = _tool_by_name(exs_mod.build(ctx), "worktree")

    result = await wt_tool.handler({"op": "new", "name": "demo", "base": "--force"})
    assert result.get("isError")
    assert "force" in result["content"][0]["text"].lower()
    assert ctx.state.active_worktree is None

    result2 = await wt_tool.handler({"op": "remove", "name": "demo; --force"})
    assert result2.get("isError")


# ---------------------------------------------------------------------------
# test(device=...)
# ---------------------------------------------------------------------------


async def test_test_rejects_unknown_device(paths, tmp_path):
    ctx = _make_ctx(paths, tmp_path)
    test_tool = _tool_by_name(exs_mod.build(ctx), "test")

    result = await test_tool.handler({"device": "rocm"})
    assert result.get("isError")
    assert "rocm" in result["content"][0]["text"]
