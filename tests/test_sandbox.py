"""sandbox.py: the bwrap-argv builder (unit-level, with `available`
monkeypatched), plus real sandbox behaviour against the actual `bwrap`
binary when one is present on PATH, plus the same behaviour exercised
through `WorkerManager`/`py.eval` -- the real path a model actually uses.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from lictor import sandbox
from lictor.config import Config
from lictor.options import State
from lictor.workers import WorkerManager

#: Whether a real `bwrap` is available on this machine. The unit tests
#: below (flag construction, off/require/auto branching) monkeypatch
#: `sandbox.available` and need no real binary; everything that actually
#: *runs* a sandboxed process is skipped outright when there is none,
#: rather than silently asserting nothing.
_REAL_BWRAP = sandbox.available()
requires_bwrap = pytest.mark.skipif(
    _REAL_BWRAP is None,
    reason="bwrap not found on PATH; real sandbox behaviour cannot be exercised",
)


# ---------------------------------------------------------------------------
# wrap() / mode() / describe() -- pure argv construction, no real bwrap needed
# ---------------------------------------------------------------------------


def test_mode_defaults_to_auto_when_cfg_is_none() -> None:
    assert sandbox.mode(None) == "auto"


def test_mode_reads_cfg_sandbox_bwrap() -> None:
    cfg = Config()
    cfg.sandbox.bwrap = "off"
    assert sandbox.mode(cfg) == "off"


def test_wrap_off_returns_argv_unchanged() -> None:
    cfg = Config()
    cfg.sandbox.bwrap = "off"
    argv = ["echo", "hi"]
    assert sandbox.wrap(argv, cfg=cfg) == ["echo", "hi"]


def test_wrap_require_raises_when_bwrap_missing(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: None)
    cfg = Config()
    cfg.sandbox.bwrap = "require"
    with pytest.raises(sandbox.SandboxUnavailable, match="require"):
        sandbox.wrap(["echo", "hi"], cfg=cfg)


def test_wrap_auto_missing_warns_once_and_returns_unchanged(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: None)
    monkeypatch.setattr(sandbox, "_warned_missing", False)
    cfg = Config()  # default mode is "auto"

    with pytest.warns(RuntimeWarning, match="bwrap"):
        first = sandbox.wrap(["echo", "hi"], cfg=cfg)
    assert first == ["echo", "hi"]

    # second call in the same process: no second warning (warn-once).
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        second = sandbox.wrap(["echo", "hi"], cfg=cfg)
    assert second == ["echo", "hi"]


def test_wrap_builds_expected_flags_in_auto_with_bwrap_present(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: "/fake/bin/bwrap")
    cfg = Config()  # auto
    writable_dir = tmp_path / "work"
    writable_dir.mkdir()

    result = sandbox.wrap(["python3", "-c", "pass"], writable=(writable_dir,), network=False, cfg=cfg)

    assert result == [
        "/fake/bin/bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", str(writable_dir), str(writable_dir),
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--",
        "python3", "-c", "pass",
    ]


def test_wrap_network_true_omits_unshare_net(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: "/fake/bin/bwrap")
    cfg = Config()
    result = sandbox.wrap(["true"], network=True, cfg=cfg)
    assert "--unshare-net" not in result
    assert "--unshare-pid" in result  # the rest of the policy still applies


def test_wrap_skips_writable_paths_that_do_not_exist(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: "/fake/bin/bwrap")
    cfg = Config()
    missing = tmp_path / "does-not-exist"
    result = sandbox.wrap(["true"], writable=(missing,), cfg=cfg)
    assert str(missing) not in result


def test_describe_mentions_mode_and_bwrap_path(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: "/fake/bin/bwrap")
    cfg = Config()
    out = sandbox.describe(cfg)
    assert "auto" in out
    assert "/fake/bin/bwrap" in out


def test_describe_off() -> None:
    cfg = Config()
    cfg.sandbox.bwrap = "off"
    assert "off" in sandbox.describe(cfg)


def test_describe_require_missing(monkeypatch) -> None:
    monkeypatch.setattr(sandbox, "available", lambda: None)
    cfg = Config()
    cfg.sandbox.bwrap = "require"
    out = sandbox.describe(cfg)
    assert "require" in out and "refuse" in out


# ---------------------------------------------------------------------------
# real sandbox behaviour -- actual bwrap, actual subprocesses
# ---------------------------------------------------------------------------


@requires_bwrap
def test_real_sandbox_can_read_store_and_run_python(tmp_path) -> None:
    """The interpreter itself lives under /nix/store in this dev shell;
    if --ro-bind / / did not leave it readable, this would fail to start
    at all rather than print anything."""
    argv = sandbox.wrap([sys.executable, "-c", "print('ok')"], writable=(tmp_path,), cfg=Config())
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


@requires_bwrap
def test_real_sandbox_can_write_under_writable_path(tmp_path) -> None:
    target = tmp_path / "ok.txt"
    code = f"open({str(target)!r}, 'w').write('hi')"
    argv = sandbox.wrap([sys.executable, "-c", code], writable=(tmp_path,), cfg=Config())
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert target.read_text() == "hi"


@requires_bwrap
def test_real_sandbox_cannot_write_outside_writable_path(tmp_path) -> None:
    code = "open('/etc/lictor-should-not-exist', 'w')"
    argv = sandbox.wrap([sys.executable, "-c", code], writable=(tmp_path,), cfg=Config())
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0
    assert "Read-only file system" in proc.stderr


@requires_bwrap
def test_real_sandbox_cannot_open_socket(tmp_path) -> None:
    code = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=2)"
    argv = sandbox.wrap([sys.executable, "-c", code], writable=(tmp_path,), cfg=Config())
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    assert proc.returncode != 0
    assert "Network is unreachable" in proc.stderr


# ---------------------------------------------------------------------------
# through WorkerManager / py.eval -- the real path a model actually uses
# ---------------------------------------------------------------------------


def _manager(cfg, paths, tmp_path, cid: str) -> WorkerManager:
    state = State(cwd=tmp_path, workspace=tmp_path, active_worktree=None, cid=cid)
    return WorkerManager(cfg=cfg, state=state, records=None, paths=paths)


@requires_bwrap
async def test_worker_marks_itself_sandboxed_and_still_evaluates(paths, tmp_path) -> None:
    manager = _manager(Config(), paths, tmp_path, "sbx-basic")
    await manager.start("w_basic")
    try:
        rows = {row["name"]: row for row in manager.repls()}
        assert rows["w_basic"]["sandboxed"] is True

        result = await manager.eval("w_basic", "1 + 1", timeout=10)
        assert result["ok"] is True
        assert result["value"] == "2"
    finally:
        await manager.shutdown_all()


@requires_bwrap
async def test_worker_namespace_persists_and_writes_under_workspace(paths, tmp_path) -> None:
    manager = _manager(Config(), paths, tmp_path, "sbx-write-in")
    await manager.start("w_in")
    try:
        target = tmp_path / "probe.txt"
        code1 = f"path = {str(target)!r}\nopen(path, 'w').write('hi')\n'wrote'"
        first = await manager.eval("w_in", code1, timeout=10)
        assert first["ok"] is True
        assert first["value"] == "'wrote'"
        assert target.read_text() == "hi"

        # same worker, second eval: `path` is still bound from the first call.
        second = await manager.eval("w_in", "path", timeout=10)
        assert second["ok"] is True
        assert str(target) in second["value"]
    finally:
        await manager.shutdown_all()


@requires_bwrap
async def test_worker_write_outside_workspace_denied(paths, tmp_path) -> None:
    manager = _manager(Config(), paths, tmp_path, "sbx-write-out")
    await manager.start("w_out")
    try:
        code = (
            "try:\n"
            "    open('/etc/lictor-should-not-exist', 'w')\n"
            "    result = 'wrote'\n"
            "except OSError as e:\n"
            "    result = repr(e)\n"
            "result\n"
        )
        result = await manager.eval("w_out", code, timeout=10)
        assert result["ok"] is True
        assert result["value"] != "'wrote'"
        assert "Read-only file system" in result["value"]
    finally:
        await manager.shutdown_all()


@requires_bwrap
async def test_worker_network_denied_through_eval(paths, tmp_path) -> None:
    """The real demonstration: py.eval of a socket connection fails under
    the sandbox, through the exact path a model would use."""
    manager = _manager(Config(), paths, tmp_path, "sbx-net")
    await manager.start("w_net")
    try:
        code = (
            "import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=2)\n"
            "    result = 'connected'\n"
            "except OSError as e:\n"
            "    result = repr(e)\n"
            "result\n"
        )
        result = await manager.eval("w_net", code, timeout=10)
        assert result["ok"] is True
        assert "connected" not in result["value"]
        assert "Network is unreachable" in result["value"]
    finally:
        await manager.shutdown_all()


@requires_bwrap
async def test_worker_nameerror_and_timeout_kill_and_restart_under_sandbox(paths, tmp_path) -> None:
    manager = _manager(Config(), paths, tmp_path, "sbx-full")
    await manager.start("w_full")
    try:
        bad = await manager.eval("w_full", "undefined_name_xyz", timeout=10)
        assert bad["ok"] is False
        assert bad["error"]["type"] == "NameError"

        timed_out = await manager.eval("w_full", "while True: pass", timeout=1)
        assert timed_out["ok"] is False
        assert timed_out["error"]["type"] == "Timeout"

        rows = {row["name"]: row for row in manager.repls()}
        assert rows["w_full"]["alive"] is False

        restarted = await manager.eval("w_full", "1 + 1", timeout=10)
        assert restarted["ok"] is True
        assert restarted["value"] == "2"
        assert "restarted" in restarted

        rows = {row["name"]: row for row in manager.repls()}
        assert rows["w_full"]["alive"] is True
        assert rows["w_full"]["sandboxed"] is True
    finally:
        await manager.shutdown_all()
