from __future__ import annotations

import asyncio

import pytest

from lictor.workers import WorkerManager


@pytest.fixture
def manager(paths):
    return WorkerManager(cfg=None, state=None, records=None, paths=paths)


async def _shutdown(mgr: WorkerManager) -> None:
    await mgr.shutdown_all()


async def test_state_persists_across_two_evals(manager):
    await manager.start("w1")
    try:
        first = await manager.eval("w1", "x = 1\nx + 1")
        assert first["ok"] is True
        assert first["value"] == "2"

        second = await manager.eval("w1", "x + 41")
        assert second["ok"] is True
        assert second["value"] == "42"
    finally:
        await _shutdown(manager)


async def test_name_error_is_structured_not_raised(manager):
    await manager.start("w2")
    try:
        result = await manager.eval("w2", "undefined_name_xyz")
        assert result["ok"] is False
        assert result["error"]["type"] == "NameError"
        assert "undefined_name_xyz" in result["error"]["message"]
    finally:
        await _shutdown(manager)


async def test_timeout_kills_and_restarts(manager):
    await manager.start("w3")
    try:
        timed_out = await manager.eval("w3", "while True: pass", timeout=1)
        assert timed_out["ok"] is False
        assert timed_out["error"]["type"] == "Timeout"

        rows = {row["name"]: row for row in manager.repls()}
        assert rows["w3"]["alive"] is False

        restarted = await manager.eval("w3", "1 + 1")
        assert restarted["ok"] is True
        assert restarted["value"] == "2"
        assert "restarted" in restarted

        rows = {row["name"]: row for row in manager.repls()}
        assert rows["w3"]["alive"] is True
    finally:
        await _shutdown(manager)


async def test_child_env_has_no_claude_vars(manager):
    await manager.start("w4")
    try:
        result = await manager.eval(
            "w4", "import os\n[k for k in os.environ if k.startswith('CLAUDE')]"
        )
        assert result["ok"] is True
        assert result["value"] == "[]"
    finally:
        await _shutdown(manager)


async def test_concurrent_evals_do_not_interleave(manager):
    await manager.start("w5")
    try:
        a, b = await asyncio.gather(
            manager.eval("w5", "import time\ntime.sleep(0.2)\n1 + 1"),
            manager.eval("w5", "10 + 10"),
        )
        assert a["ok"] is True and a["value"] == "2"
        assert b["ok"] is True and b["value"] == "20"
    finally:
        await _shutdown(manager)


async def test_stdout_stderr_captured(manager):
    await manager.start("w6")
    try:
        result = await manager.eval("w6", "print('hello')\n1")
        assert result["ok"] is True
        assert result["stdout"] == "hello\n"
    finally:
        await _shutdown(manager)


async def test_reset_clears_namespace(manager):
    await manager.start("w7")
    try:
        await manager.eval("w7", "x = 99")
        await manager.reset("w7")
        result = await manager.eval("w7", "x")
        assert result["ok"] is False
        assert result["error"]["type"] == "NameError"
    finally:
        await _shutdown(manager)


async def test_start_same_name_twice_errors(manager):
    await manager.start("w8")
    try:
        with pytest.raises(ValueError):
            await manager.start("w8")
    finally:
        await _shutdown(manager)
