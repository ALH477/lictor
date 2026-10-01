"""The ``py.*`` worker child process: ``python -m lictor.worker_main``.

**stdlib only.** This module must never import anything from ``lictor``
itself -- it runs as an untrusted, isolated subprocess, started by
``lictor.workers.WorkerManager`` with a scrubbed environment and its own
process group, and anything it evaluates is code a *user* handed to
``py.eval``. A worker never evaluates lictor's own code; redefining
lictor's own behaviour live is what ``self.*`` (``lictor/image.py``) is
for. Keeping this module import-free of ``lictor`` is what makes that
separation checkable rather than promised: there is nothing of lictor's
in this process for user code to reach through ``sys.modules``.

Protocol: line-delimited JSON on stdin/stdout, one persistent namespace
dict for the life of the process::

    -> {"id": 1, "op": "eval", "code": "x = 1\\nx + 1", "timeout": 30}
    <- {"id": 1, "ok": true, "value": "2", "stdout": "", "stderr": "", "ms": 1}
    <- {"id": 1, "ok": false, "error": {"type": "NameError", "message": "...", "traceback": "..."}}

``ops``: ``eval`` | ``reset`` | ``ping`` | ``shutdown``.

``eval`` uses the exec-all-but-trailing-expression rule: if the code
parses to a module whose last statement is a bare expression, every
statement before it is ``exec``'d and that last expression is
``eval``'d, so ``"x = 1\\nx + 1"`` both binds ``x`` in the persistent
namespace and reports ``2``. The *object* is never returned over the
wire -- only ``repr(value)`` is, since JSON cannot carry an arbitrary
Python object and the parent process must not unpickle anything from
here. stdout/stderr produced while running are captured and returned
as text, not interleaved with the protocol stream.

This process enforces no timeout on itself (a worker running
``while True: pass`` cannot cooperatively notice a deadline). The
parent is responsible for killing the whole process group when a call
runs too long; a killed worker is simply gone, and the manager starts a
fresh one with a fresh namespace.
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import sys
import time
import traceback
from typing import Any


def _fresh_namespace() -> dict[str, Any]:
    return {"__name__": "__worker__", "__builtins__": __builtins__}


def _split_last_expression(code: str) -> tuple[Any, Any]:
    """Parse `code`; return (exec_code, eval_code), either of which may
    be ``None``. ``eval_code`` is set only when the module's last
    statement is a bare expression."""
    tree = ast.parse(code, mode="exec")
    eval_code = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = tree.body.pop()
        expr = ast.Expression(body=last.value)
        ast.copy_location(expr, last.value)
        ast.fix_missing_locations(expr)
        eval_code = compile(expr, "<worker>", "eval")
    exec_code = compile(tree, "<worker>", "exec")
    return exec_code, eval_code


def _run_eval(namespace: dict[str, Any], code: str) -> dict[str, Any]:
    t0 = time.monotonic()
    out = io.StringIO()
    err = io.StringIO()
    try:
        exec_code, eval_code = _split_last_expression(code)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(exec_code, namespace)  # noqa: S102 - this *is* py.eval; code is the user's own request
            value = eval(eval_code, namespace) if eval_code is not None else None
        ms = int((time.monotonic() - t0) * 1000)
        return {
            "ok": True,
            "value": repr(value),
            "stdout": out.getvalue(),
            "stderr": err.getvalue(),
            "ms": ms,
        }
    except SyntaxError as exc:
        return {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()},
            "stdout": out.getvalue(),
            "stderr": err.getvalue(),
        }
    except BaseException as exc:  # noqa: BLE001 - the whole point is to report any failure
        return {
            "ok": False,
            "error": {"type": type(exc).__name__, "message": str(exc), "traceback": traceback.format_exc()},
            "stdout": out.getvalue(),
            "stderr": err.getvalue(),
        }


def _respond(req_id: Any, payload: dict[str, Any]) -> None:
    payload = {"id": req_id, **payload}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    namespace = _fresh_namespace()
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            _respond(None, {"ok": False, "error": {"type": "ProtocolError", "message": str(exc), "traceback": ""}})
            continue

        req_id = msg.get("id")
        op = msg.get("op")

        if op == "ping":
            _respond(req_id, {"ok": True, "value": "pong"})
        elif op == "reset":
            namespace = _fresh_namespace()
            _respond(req_id, {"ok": True, "value": "None"})
        elif op == "shutdown":
            _respond(req_id, {"ok": True, "value": "None"})
            return 0
        elif op == "eval":
            result = _run_eval(namespace, msg.get("code", ""))
            _respond(req_id, result)
        else:
            _respond(
                req_id,
                {"ok": False, "error": {"type": "ValueError", "message": f"unknown op {op!r}", "traceback": ""}},
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
