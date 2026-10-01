# Architecture

lictor is one Python process with two threads, driving one `claude` subprocess,
with a handful of further subprocesses hanging off it for isolation. This
document is the shape of that, and the three design decisions that are not
obvious from reading any single module: why the threading is inverted, why
there is one MCP server per namespace instead of one for all of lictor's
tools, and why the tool registry stores SDK objects rather than plain
functions.

## The shape of the process tree

```
 terminal (your tty)
   │  SIGINT lands HERE, nowhere else
   ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ lictor, main thread                                            │
 │   app.py: readline input() loop / run_once()                   │
 │   permissions.py: Approvals.service_pending() -- answers       │
 │     approval prompts the loop thread is blocked waiting on     │
 └───────────────────────────────────────────────────────────────┘
   │  asyncio.run_coroutine_threadsafe (App.call)
   ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ lictor, daemon asyncio-loop thread ("lictor-brain-loop")        │
 │   brain.py: Brain owns ClaudeSDKClient, runs one turn at a time│
 │   permissions.py: Approvals.can_use_tool (awaits the main      │
 │     thread's answer via a thread-safe Future)                  │
 │   hooks.py: PreToolUse/PostToolUse callbacks                   │
 │   tools/*.py: exs.* self.* py.* rlm.* resource.* handlers,     │
 │     running IN this process, registered on a ToolRegistry      │
 │     (lictor/tools/__init__.py) -- one SdkMcpTool per tool,     │
 │     one in-process MCP server per namespace (see below)        │
 │   image.py / overlay.py: the live image self.* mutates         │
 └───────────────────────────────────────────────────────────────┘
   │  ClaudeSDKClient (stdio, JSON control protocol)
   ▼
 ┌───────────────────────────────────────────────────────────────┐
 │ `claude` binary, subprocess (the user's own Claude Code,       │
 │ CLAUDE_CODE_ENTRYPOINT=sdk-py, subscription login, not a key)  │
 │   built-in Read/Edit/Bash/Glob/Grep/TodoWrite/Agent/WebFetch,  │
 │   fenced by hooks.py and gated by permissions.py               │
 │   mcp__exs__*, mcp__self__*, mcp__py__*, mcp__rlm__*,          │
 │   mcp__resource__* -- calls back INTO the loop thread above,   │
 │   over the in-process MCP servers built by ToolRegistry.servers│
 └───────────────────────────────────────────────────────────────┘
   │ tool handlers that need isolation shell out further:
   ├─► workers.py -> `python -m lictor.worker_main` (py.* backing;
   │     stdlib only, its own process group, scrubbed env, no
   │     lictor import at all -- see "the boundary that matters" below)
   ├─► tools/exs.py -> `nix develop -c make/tests/run.sh/...`
   │     (bounded asyncio.create_subprocess_exec, own process group)
   └─► overlay.py -> `python -m lictor.probe` (the replay probe, a
         clean subprocess with a scrubbed env, launched only from
         self.commit before anything touches mutations.git)

 XDG-ish state, read and written by both threads above (never by a
 worker or a `claude` subprocess, which only see it indirectly through
 a tool result):
   $LICTOR_STATE_HOME/lictor/   conversations/ vault/ overlay/
                                 generations/ crash/ artifacts/
                                 mutations.git/ active-generation
                                 boot.json  lictor.log  history
   $LICTOR_DATA_HOME/lictor/    inferences/
   $LICTOR_CONFIG_HOME/lictor/  config.toml  agents/*.md
```

## Why the threading is inverted

Python delivers `SIGINT` to one thread: whichever one the interpreter
happens to be running when the signal arrives is not under lictor's
control, but in CPython a plain, un-redirected `KeyboardInterrupt` only
ever raises on the **main** thread -- a background thread's code simply
keeps running. If lictor ran its terminal loop on a background thread and
let `asyncio.run()` own the main thread (the naive way to write an asyncio
program), Ctrl-C would have nowhere useful to land: it could not interrupt
`input()`, because `input()` would not be on the main thread, and asyncio's
own signal handling assumes it owns the main thread too.

So `app.py` inverts the usual shape: the **main** thread runs the blocking
`readline`/`input()` loop (`App.run`), and a **daemon** thread
(`App.start_loop` / `App._loop_main`) owns the asyncio event loop that
`Brain` and `ClaudeSDKClient` live on. A Ctrl-C now always lands on the main
thread, in one of exactly two places: blocked in `input()` at an empty
prompt (readline's own handling clears the line, or a second one exits),
or blocked in `App.wait_turn`'s `fut.result(timeout=0.1)` poll while a turn
is in flight on the loop thread -- where it is caught and turned into
`self.call(self.brain.interrupt())`, scheduled onto the loop thread, while
the main thread goes right back to polling the same future. Nothing on the
loop thread ever needs to handle `SIGINT` itself; it only ever receives an
interrupt as an ordinary coroutine call, requested by the one thread that
can actually see the signal.

## Why one MCP server per namespace

The Agent SDK's wire tool names must match `^[a-zA-Z0-9_-]+$` (enforced in
`ToolRegistry.register`, `lictor/tools/__init__.py`) -- a dot is not a
legal character in a tool name on the wire. But lictor wants its tools
*read* as `exs.build`, `self.redefine`, `py.eval`: a dotted, namespaced
vocabulary, the same shape autolith's own tool names have. An MCP server
exposes tools as a flat list with no namespace of its own; the SDK derives
the wire name `mcp__<server>__<tool>` from the server's name and the
tool's own name. So lictor gets the dot back by making the **server**
the namespace: one `create_sdk_mcp_server("exs", ...)` holding every
`exs.*` tool as a plain `build`/`test`/`spec`/... name, one for `self`,
one for `py`, and so on. `exs.build` is registered as dotted-key `"exs.build"`
in the registry, reaches the model as `mcp__exs__build`, and is spoken of
as `exs.build` everywhere a person reads it -- the registry's `wire_id()`
and `tool_ids()` are the two directions of that translation.

## Why the registry holds `SdkMcpTool` objects, not bound functions

`create_sdk_mcp_server` closes over whatever list of tool objects it is
given, and -- this is the detail the whole `self.*` mutation pipeline
depends on -- it reads `tool_def.handler` fresh, **at call time**, not at
server-construction time. So the registry (`ToolRegistry.tools`, a
`dict[str, SdkMcpTool]`) keeps the actual `SdkMcpTool` objects the `@tool`
decorator produced, rather than unwrapping them down to plain async
functions. `ToolRegistry.rebind(dotted, handler)` just does
`sdk_tool.handler = handler` on that live object; because the server
already closed over the object itself (not a snapshot of its handler),
every subsequent call to that tool sees the new handler immediately, on
a session that is still connected, with no reconnect. This is exactly
what lets `overlay.apply`'s `kind == "tool"` branch (`lictor/overlay.py`)
redefine a tool's behavior live. The one thing this mechanism cannot
touch is the tool's **schema**: the wire tool list and its JSON schemas
are fixed once, when `ToolRegistry.servers()` builds the `create_sdk_mcp_server`
call, so a redefine whose new `@tool` declares a different schema is
refused outright (`overlay.apply`, "a schema change needs a restart") --
rebinding a handler can change what a tool does, never what it looks like
on the wire.

## The boundary that matters: `py.*` cannot touch the agent image, `self.*` can

Both `py.*` and `self.*` let the model run arbitrary code, and it would be
easy to assume they are the same kind of risk with different plumbing. They
are not, and the difference is the entire reason `self.*` (not `py.*`) is
what makes lictor autolith-like rather than merely scriptable.

A `py.*` worker (`lictor/workers.py`, backed by `lictor/worker_main.py`) is
a **separate OS process**: `python -m lictor.worker_main`, started with
`start_new_session=True` (its own process group, so a timeout's `killpg`
takes any children the worker's own code forked with it), a scrubbed
environment (`workers._scrubbed_env`, the same `options.scrub_environ` rule
applied to a fresh copy), and -- the thing that actually enforces the
boundary -- `worker_main.py` imports **nothing from the `lictor` package**.
It is stdlib-only by construction. Whatever the model evaluates in a `py.*`
REPL runs in a process that has no `lictor.overlay`, no `lictor.tools`, no
`ToolRegistry` to import and mutate even if it tried; the worst a runaway
`py.eval` can do to *lictor itself* is nothing, because there is no path
back into the parent process's memory. It can misbehave arbitrarily inside
its own namespace, and a `while True: pass` or a hang is handled by
`WorkerManager._kill` (`killpg` + restart on the next `eval`) -- a resource
problem, not an image-integrity one.

`self.*` (`lictor/tools/self_.py`, `lictor/image.py`, `lictor/overlay.py`)
is the opposite shape on purpose: it runs **in lictor's own process**, on
the same loop thread as everything else, and `overlay.apply` compiles and
`exec`s the operator's source directly against the running
`ToolRegistry` or a `lictor.*` module's own `__dict__`
(`setattr(target_module, attr, new_value)`). That is the only sanctioned
way anything reaches back into lictor's live image: there is no second,
accidental path, because nothing else in the tool registry is given a
handle to the registry object, the loaded modules, or `sys.modules` the
way `ToolContext.registry`/`ToolContext.image` are wired up specifically
for `self.*`'s handlers. The safety net on this side is not process
isolation -- it is the overlay journal's own pipeline (see
`docs/mutations.md`): nothing is durable until `self.exercise` then
`self.commit`, and `self.commit` always re-verifies the result in a
**clean subprocess** (`python -m lictor.probe`) before it is allowed to
touch `mutations.git`. `py.*`'s isolation is a process boundary; `self.*`'s
is a review-before-commit pipeline, because the entire point of `self.*`
is that it is allowed to cross the boundary `py.*` is built to never reach.
