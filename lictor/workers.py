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

    async def _spawn(self, name: str, cwd: Path | None) -> _Worker:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "lictor.worker_main",
            cwd=str(cwd) if cwd else None,
            env=_scrubbed_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, for killpg
        )
        return _Worker(name=name, cwd=cwd, proc=proc)

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
                }
            )
        return rows
