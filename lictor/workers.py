"""``WorkerManager``: isolated ``py.*`` REPLs.

Each worker is a ``python -m lictor.worker_main`` child (stdlib only --
see that module's docstring for why) with its own process group and a
scrubbed environment, speaking the line-delimited JSON protocol
documented there. Everything here is ``asyncio``: a blocking call would
stall the same loop that services tool-approval requests and hooks, so
every read/write against a worker's pipes goes through
``asyncio.StreamWriter``/``StreamReader`` with an explicit timeout, and
a per-worker ``asyncio.Lock`` serialises concurrent ``eval`` calls onto
one pipe so two callers can never interleave on it.

A worker that overruns its timeout is killed by process group
(``os.killpg``, so a child the user's code itself forked dies too),
marked dead, and silently restarted -- with a note saying so -- the
next time anyone calls ``eval`` on that name.

**Sandboxing (M6) and what it does to ``os.killpg``.** Every worker's
argv is routed through ``sandbox.wrap`` (no network, host read-only,
workspace + lictor state dir writable -- see ``lictor/sandbox.py`` for
the full policy and why ``py.*`` is sandboxed while ``exs.*`` is not).
When that wrapping is active, the process asyncio actually spawns is
`bwrap`, not `python` directly, and `bwrap` internally forks an *inner*
monitor process that ends up in a **different, second process group**
from the outer `bwrap` invocation -- confirmed empirically on this
machine (bubblewrap 0.11.2): spawning `bwrap --unshare-pid ... -- sleep
100` with `start_new_session=True` and inspecting `ps` shows the outer
`bwrap` in pgid P (matching `os.getpgid` of the spawned pid, as before)
and a second `bwrap` + the sandboxed command itself in a *different*
pgid, one level down. `os.killpg(P, SIGKILL)` -- the existing call below
-- therefore does **not** directly signal the sandboxed command; it only
directly kills the outer `bwrap`. What makes the kill still reach the
sandboxed process tree is `--die-with-parent`, which `sandbox.wrap`
always includes: with it, killing the outer `bwrap` cascades to the
inner monitor and the sandboxed command (confirmed: the whole tree
disappears from `ps` within the same tick). Without `--die-with-parent`
(tested by removing just that flag), the inner `bwrap` + sandboxed
command survive, reparented to pid 1, orphaned. So the existing
`os.killpg` call needed **no code change** -- but it is now correct only
*because* `sandbox.wrap` never omits `--die-with-parent`; if that flag
were ever dropped from the sandbox policy, timeout-kill would silently
stop reaping sandboxed workers.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import sandbox
from .options import scrub_environ

#: Used when a caller does not pass one.
DEFAULT_TIMEOUT = 30.0

#: How much longer than the caller's timeout the manager waits for the
#: child to notice and respond before giving up and killing it anyway.
_KILL_GRACE_S = 2.0

#: The directory containing the ``lictor`` package itself, i.e. the repo
#: root in a checkout. Prepended to the child's PYTHONPATH so
#: ``python -m lictor.worker_main`` resolves regardless of the worker's
#: own `cwd` -- needed in an unpackaged checkout (a `nix build` install
#: already has lictor on sys.path, so this is a harmless no-op there).
_LICTOR_PARENT = str(Path(__file__).resolve().parent.parent)


def _scrubbed_env() -> dict[str, str]:
    """A copy of the current environment with the Claude Code session
    variables removed, per ``options.scrub_environ``'s key rule -- a
    worker must never see the keys that would make it think it's a
    nested Claude Code / lictor process."""
    env = dict(os.environ)
    scrub_environ(env)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{_LICTOR_PARENT}{os.pathsep}{existing}" if existing else _LICTOR_PARENT
    return env


@dataclass
class _Worker:
    name: str
    cwd: Path | None
    proc: asyncio.subprocess.Process
    sandboxed: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    alive: bool = True
    last_used: float = field(default_factory=time.time)
    eval_count: int = 0
    next_id: int = 1


class WorkerManager:
    def __init__(self, cfg: Any, state: Any, records: Any, paths: Any) -> None:
        self.cfg = cfg
        self.state = state
        self.records = records
        self.paths = paths
        self._workers: dict[str, _Worker] = {}

    # -- spawning ---------------------------------------------------------

    def _writable_paths(self, cwd: Path | None) -> tuple[Path, ...]:
        """Paths a sandboxed worker needs write access to: the workspace
        (so edits/scratch files under it work) and lictor's own state dir
        (so a worker that shells back out to lictor-adjacent tooling can
        still see it) -- plus the worker's own `cwd` if one was given and
        it is not already one of those two, since a worker started in a
        worktree needs to write there even though that path is normally
        nested under the workspace anyway."""
        seen: list[Path] = []
        if self.state is not None and getattr(self.state, "workspace", None):
            seen.append(Path(self.state.workspace))
        if self.paths is not None and getattr(self.paths, "state", None):
            seen.append(Path(self.paths.state))
        if cwd is not None and cwd not in seen:
            seen.append(cwd)
        return tuple(seen)

    async def _spawn(self, name: str, cwd: Path | None) -> _Worker:
        argv = [sys.executable, "-m", "lictor.worker_main"]
        bwrap_path = sandbox.available()
        sandbox_mode = sandbox.mode(self.cfg)
        wrapped = sandbox.wrap(
            argv,
            writable=self._writable_paths(cwd),
            network=False,
            cfg=self.cfg,
        )
        sandboxed = sandbox_mode != "off" and bwrap_path is not None
        proc = await asyncio.create_subprocess_exec(
            *wrapped,
            cwd=str(cwd) if cwd else None,
            env=_scrubbed_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, for killpg
        )
        return _Worker(name=name, cwd=cwd, proc=proc, sandboxed=sandboxed)

    async def start(self, name: str, cwd: str | Path | None = None) -> _Worker:
        existing = self._workers.get(name)
        if existing is not None and existing.alive:
            raise ValueError(f"worker {name!r} is already running")
        worker = await self._spawn(name, Path(cwd) if cwd else None)
        self._workers[name] = worker
        return worker

    # -- killing ------------------------------------------------------

    async def _kill(self, worker: _Worker) -> None:
        worker.alive = False
        try:
            pgid = os.getpgid(worker.proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(worker.proc.wait(), timeout=5)
        except TimeoutError:
            pass

    async def stop(self, name: str) -> None:
        worker = self._workers.get(name)
        if worker is None:
            raise KeyError(name)
        if worker.alive:
            await self._kill(worker)
        del self._workers[name]

    async def shutdown_all(self) -> None:
        for name in list(self._workers):
            await self.stop(name)

    # -- the wire protocol, one request/response pair at a time -----------

    async def _request(self, worker: _Worker, op: str, wait_timeout: float, **fields: Any) -> dict[str, Any]:
        req_id = worker.next_id
        worker.next_id += 1
        payload = json.dumps({"id": req_id, "op": op, **fields}, ensure_ascii=False) + "\n"
        async with worker.lock:
            try:
                worker.proc.stdin.write(payload.encode("utf-8"))
                await worker.proc.stdin.drain()
                line = await asyncio.wait_for(worker.proc.stdout.readline(), timeout=wait_timeout)
            except (TimeoutError, BrokenPipeError, ConnectionResetError):
                await self._kill(worker)
                return {
                    "ok": False,
                    "error": {
                        "type": "Timeout",
                        "message": f"worker {worker.name!r} exceeded {wait_timeout}s and was killed",
                    },
                }
            if not line:
                await self._kill(worker)
                return {"ok": False, "error": {"type": "EOF", "message": f"worker {worker.name!r} exited"}}
            worker.last_used = time.time()
            try:
                return json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as exc:
                return {"ok": False, "error": {"type": "ProtocolError", "message": str(exc)}}

    async def eval(self, name: str, code: str, timeout: float | None = None) -> dict[str, Any]:
        timeout = DEFAULT_TIMEOUT if timeout is None else timeout
        note: str | None = None

        worker = self._workers.get(name)
        if worker is None:
            worker = await self._spawn(name, None)
            self._workers[name] = worker
            note = f"worker {name!r} was not running; started fresh."
        elif not worker.alive:
            worker = await self._spawn(name, worker.cwd)
            self._workers[name] = worker
            note = f"worker {name!r} was dead (killed after a timeout); restarted with a fresh namespace."

        response = await self._request(worker, "eval", timeout + _KILL_GRACE_S, code=code, timeout=timeout)
        if worker.alive:
            worker.eval_count += 1
        if note:
            response = {**response, "restarted": note}
        return response

    async def reset(self, name: str) -> dict[str, Any]:
        worker = self._workers.get(name)
        if worker is None or not worker.alive:
            raise KeyError(name)
        return await self._request(worker, "reset", DEFAULT_TIMEOUT)

    # -- introspection -------------------------------------------------

    def repls(self) -> list[dict[str, Any]]:
        rows = []
        for name, worker in self._workers.items():
            rows.append(
                {
                    "name": name,
                    "pid": worker.proc.pid,
                    "alive": worker.alive,
                    "last_used": worker.last_used,
                    "eval_count": worker.eval_count,
                    "sandboxed": worker.sandboxed,
                }
            )
        return rows
