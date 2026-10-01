"""App: the main-thread terminal loop, plus the daemon asyncio-loop thread
that owns the Brain / ClaudeSDKClient.

Threading model (this is the whole reason the module is shaped this way):
SIGINT only ever reaches the main thread. A Ctrl-C while blocked in
``input()`` at an empty prompt is a clean-shutdown request. A Ctrl-C while
blocked in ``wait_turn`` (a turn is in flight on the loop thread) is routed
to ``brain.interrupt()`` on the loop thread, and the main thread keeps
waiting for that same turn's future to resolve.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from pathlib import Path
from typing import Any

from . import repl
from .repl import Command, ImageForm, Submission


class App:
    def __init__(self, cfg: Any, state: Any, records: Any, approvals: Any, brain: Any, renderer: Any) -> None:
        self.cfg = cfg
        self.state = state
        self.records = records
        self.approvals = approvals
        self.brain = brain
        self.renderer = renderer
        self.loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    # -- loop-thread plumbing --------------------------------------------

    def _loop_main(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        loop.run_forever()

    def start_loop(self) -> None:
        self._thread = threading.Thread(target=self._loop_main, daemon=True, name="lictor-brain-loop")
        self._thread.start()
        while self.loop is None:
            pass  # the thread sets this within microseconds of starting

    def stop_loop(self) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def call(self, coro: Any) -> concurrent.futures.Future:
        """Schedule `coro` on the loop thread. Call from the main thread."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def wait_turn(self, fut: concurrent.futures.Future) -> Any:
        """Block the main thread for `fut`, servicing approval prompts and
        routing Ctrl-C to an interrupt rather than letting it escape."""
        while True:
            try:
                return fut.result(timeout=0.1)
            except concurrent.futures.TimeoutError:
                self.approvals.service_pending()
            except KeyboardInterrupt:
                self.call(self.brain.interrupt())

    # -- interactive REPL -------------------------------------------------

    def _history_path(self) -> Path:
        return self.records.paths.state / "history"

    def _prompt_text(self) -> str:
        return "lictor(wt)> " if self.state.active_worktree else "lictor> "

    def _image_namespace(self) -> dict[str, Any]:
        def _prompt(text: str, to: str | None = None) -> Submission:
            return Submission(text, to=to, source="image")

        def _read_file(path: str) -> str:
            return Path(path).read_text(encoding="utf-8")

        return {"prompt": _prompt, "read_file": _read_file, "state": self.state, "cfg": self.cfg}

    def run(self) -> int:
        import readline

        self.start_loop()
        self.wait_turn(self.call(self.brain.start(resume=self.state.claude_session)))

        history_path = self._history_path()
        try:
            if history_path.exists():
                readline.read_history_file(str(history_path))
        except OSError:
            pass

        try:
            while True:
                try:
                    line = input(self._prompt_text())
                except EOFError:
                    print()
                    break
                except KeyboardInterrupt:
                    # Ctrl-C at an empty prompt: clean shutdown. [UNTESTED]
                    # interactively -- see the M1 report for why this can't
                    # be scripted here, and exactly which path handles it.
                    print()
                    break

                self.records.append("input", {"text": line})

                if line.strip() == "!!":
                    item = repl.classify(self._read_image_block())
                else:
                    item = repl.classify(line)

                if item is None:
                    continue
                if isinstance(item, Command):
                    if not self._dispatch_command(item):
                        break
                    continue
                if isinstance(item, ImageForm):
                    self._handle_image(item)
                    continue
                if isinstance(item, Submission):
                    self._run_turn(item)
                    continue
        finally:
            try:
                readline.write_history_file(str(history_path))
            except OSError:
                pass
            self.wait_turn(self.call(self.brain.close()))
            self.stop_loop()

        return 0

    def _read_image_block(self) -> str:
        lines: list[str] = []
        while True:
            try:
                block_line = input()
            except EOFError:
                break
            if block_line.strip() == "!!":
                break
            lines.append(block_line)
        return "\n".join(lines)

    def _handle_image(self, form: ImageForm) -> None:
        self.records.append("image", {"src": form.src})
        try:
            result = repl.eval_image(form.src, self._image_namespace())
        except Exception as exc:  # noqa: BLE001 -- surfaced to the operator, not raised
            self.records.append("error", {"where": "image", "message": repr(exc)})
            print(f"image error: {exc!r}")
            return
        if isinstance(result, Submission):
            self._run_turn(result)
        elif result is not None:
            print(repr(result))

    def _run_turn(self, sub: Submission) -> Any:
        worktree_before = self.state.active_worktree
        summary = self.wait_turn(self.call(self.brain.submit(sub)))
        if self.state.active_worktree != worktree_before:
            # exs.worktree(op="new") moved state.cwd into a fresh worktree from
            # inside a tool handler, which cannot reconnect the session itself.
            # Reconnect now, resumed at the same Claude session, so the next
            # turn's own Read/Edit/Bash see the worktree the fence enforces.
            print(f"· reconnecting into worktree {self.state.active_worktree}")
            self.wait_turn(self.call(self.brain.reconnect(cwd=self.state.active_worktree)))
        return summary

    def _dispatch_command(self, cmd: Command) -> bool:
        """Returns False to stop the REPL loop (``/quit``)."""
        self.records.append("command", {"name": cmd.name, "args": cmd.args})
        name = cmd.name

        if name in ("quit", "exit"):
            return False

        if name == "help":
            print("/help /quit /status /model M /effort E /mode M /resume ID /sessions /cost")
            print("(every other command is not available until a later wave)")
            return True

        if name == "status":
            print(
                f"cwd={self.state.cwd} workspace={self.state.workspace} "
                f"worktree={self.state.active_worktree} session={self.state.claude_session} "
                f"model={self.state.model} effort={self.state.effort} mode={self.state.mode}"
            )
            return True

        if name == "model":
            model = cmd.args.strip() or None
            self.wait_turn(self.call(self.brain.set_model(model)))
            print(f"model={model}")
            return True

        if name == "effort":
            effort = cmd.args.strip()
            self.wait_turn(self.call(self.brain.reconnect(effort=effort)))
            print(f"effort={effort}")
            return True

        if name == "mode":
            mode = cmd.args.strip()
            self.wait_turn(self.call(self.brain.set_mode(mode)))
            print(f"mode={mode}")
            return True

        if name == "resume":
            cid = cmd.args.strip()
            self.wait_turn(self.call(self.brain.reconnect(claude_session=cid)))
            print(f"resumed session={cid}")
            return True

        if name == "sessions":
            from . import records as records_mod

            for row in records_mod.list_conversations(self.records.paths):
                print(row)
            return True

        if name == "cost":
            print("cost: see the `· cost=` line printed after each turn "
                  "(wave 1 keeps no running total yet)")
            return True

        if name in repl.ALL_COMMANDS:
            print(f"/{name}: not available until a later wave")
        else:
            print(f"/{name}: unknown command (lead with a space to send literal text as prose)")
        return True

    # -- non-interactive single turn --------------------------------------

    def run_once(self, text: str) -> int:
        self.start_loop()
        try:
            self.wait_turn(self.call(self.brain.start(resume=self.state.claude_session)))
            summary = self._run_turn(Submission(text, source="prose"))
        finally:
            self.wait_turn(self.call(self.brain.close()))
            self.stop_loop()
        return 1 if summary.is_error else 0
