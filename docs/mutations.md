# Mutations: redefine → exercise → commit → generation → rollback

This is the pipeline that makes lictor autolith-like rather than merely
scriptable (`py.*` is isolated and disposable; this is the opposite --
durable and reaching back into lictor's own running image). Nothing here
ever touches the real `lictor/` source tree directly; it goes through an
**overlay** -- a separate, append-only history of proposed changes -- and
only a deliberate, separate step (`self.persist_definition`) ever writes
back into tracked source. The implementation is `lictor/overlay.py`
(the mechanics), `lictor/image.py` (the `!`-namespace seam), and
`lictor/tools/self_.py` (the MCP tools the model actually calls).

## The pipeline

1. **`self.redefine(target, source)`** (`image.Image.redefine` ->
   `overlay.redefine`) -- compiles `source` as a throwaway module
   (`lictor._overlay.<seq>`, so a traceback it raises names the overlay
   file, not some unrelated line), applies it live
   (`overlay.apply`, see "the two targets" below), and -- only on success
   -- writes the overlay source file plus a manifest with
   `status: "proposed"`. A `RedefineRefused` (bad target, schema mismatch)
   or the overlay source itself raising leaves the `.py` file on disk "for
   forensics" but writes no manifest, and the exception propagates to the
   caller.
2. **`self.exercise(target, args_json)`** (`tools/self_.py:_exercise`) --
   calls the *live* handler (the one `self.redefine` just installed)
   directly, times it, and records the outcome -- including a raised
   exception, reported as data (`{"ok": false, "detail": "TypeError: ..."}`)
   rather than propagated -- into the manifest's `exercise` field, moving
   `status` to `"exercised"`.
3. **`self.commit(message)`** (`tools/self_.py:_commit` ->
   `overlay.commit`) -- refuses unless `status == "exercised"`. On success
   it: runs the clean-process replay probe (below) over *every* already
   committed seq plus this one; if the probe fails, refuses and returns
   the probe's own report, `status` unchanged; if it passes, folds the
   overlay directory into the private `mutations.git` history
   (`git --git-dir=<state>/mutations.git --work-tree=<state>/overlay add -A
   && commit`), writes a new `generations/<N>.json`, repoints
   `active-generation` to `N`, and marks the manifest `"committed"` with
   the commit sha recorded.
4. **A later process boots** -- `__main__.main` reads `active-generation`,
   looks up that generation's `overlay_seqs`, and calls `overlay.replay`
   to re-apply each committed seq, in order, against the freshly built
   registry, before the first prompt appears.
5. **`/rollback N`** (`overlay.rollback`) -- just repoints
   `active-generation` to `N` (or `0` for "no overlay at all"). It refuses
   a generation whose own probe never passed, and does not touch any
   overlay file: overlay `.py` files are immutable once written, so a
   rollback is cheap and loses nothing -- the next boot simply replays a
   different, shorter list of seqs.

`self.diff(target)` and `self.discard(target_or_seq)` sit alongside this
without advancing it: `diff` is read-only (`difflib.unified_diff` between
the tracked AST segment and `inspect.getsource` of whatever is live right
now); `discard` reverts the live object to whatever `apply()` recorded as
`previous` in the process-local `_APPLIED` table, but only *within the
same process that applied it* -- a fresh process that replays history has
nothing to restore *to*, so discarding something from a previous process's
session is not a supported operation. `discard` refuses outright on a
`"committed"` entry: history past that point is immutable by design.
`self.persist_definition(target)` is the one step that writes the real
`lictor/` tree -- it requires `status == "committed"` first, and then
splices the overlay's AST segment for the tracked symbol directly into the
tracked source file, replacing the original lines.

## The two targets

A `target` string picks one of exactly two schemes (`overlay._split_target`
/ `overlay.split_target`):

- **`tool:<ns>.<name>`** -- rebinds a registered `SdkMcpTool`'s `.handler`
  (`ToolRegistry.rebind`). The overlay source must define exactly one
  `@tool`-decorated object (schema compared structurally against the
  live one via `overlay.schemas_match`; a mismatch refuses with "a schema
  change needs a restart", since the SDK fixes the wire tool list and its
  schemas when the MCP server was built) **or** exactly one `async def`
  (treated as a bare replacement handler, schema untouched). Anything
  else -- zero matches, or more than one of either kind -- is refused as
  malformed.
- **`fn:<module>:<attr>`** -- `setattr`s an existing attribute of an
  already-imported `lictor.*` module. Refuses a non-`lictor` module
  outright, and refuses if the target module does not already have that
  attribute (you can replace a definition, not introduce a new name this
  way) or if the overlay source does not define it either.

## `symbol_sha256`, and the exact skip message

Every overlay entry's manifest carries a `ShadowInfo`
(`path`, `file_sha256`, `symbol`, `symbol_sha256`) computed by
`overlay.compute_shadow` at `redefine()` time: it locates the **tracked**
definition (the one in the real `lictor/` tree, found by walking the
module's AST for a `tool(...)`-decorated function matching the target's
name, or a top-level `def`/`class`/assignment matching the target's
attribute), extracts its exact source segment, and hashes that segment --
not the whole file -- as `symbol_sha256`. This is the thing that answers
"is the overlay still talking about the same code it was written against?"
independent of unrelated edits elsewhere in the file.

`overlay.replay` recomputes that same shadow for the *current* tracked
definition before reapplying a committed entry, and compares hashes. On a
mismatch it never raises and never applies the entry -- it appends a
`ReplaySkip` to the report and moves on to the next seq, with this exact
message:

```
tracked definition moved since publication
```

(`overlay.py`, `replay()`, the `current_shadow.symbol_sha256 !=
entry.shadows.symbol_sha256` branch.) `__main__.main` prints one line per
skip at boot (`"· overlay seq {skip.seq} ({skip.target}) skipped:
{skip.message}"`), so an operator who edited the tracked source out from
under a committed overlay sees exactly that, rather than a silently
stale mutation or a startup crash.

## The replay probe's checks

`overlay.commit` never writes to `mutations.git` without first running
`python -m lictor.probe` (`overlay.run_probe`) in a **clean subprocess**:
a scrubbed environment (`options.scrub_environ`), its own throwaway
`_StubPaths`, no real XDG state, no SDK transport, no live `claude`
connection -- a pure in-process construction check against a disposable
tool context. Reading `lictor/probe.py` directly (the plan's module
contract described this as "four checks"; the code as it actually stands
runs more than that, and this is the actual count, not the guess):

1. **`load_registry`** -- `tools.load(ctx)` builds a fresh `ToolRegistry`
   from every namespace module, with no overlay applied yet.
2. **`apply_overlays`** -- *only run if `seqs` is non-empty* --
   `overlay.replay(paths, seqs, registry)`, and the check fails if
   **any** seq was skipped (it turns a non-empty `report.skipped` into a
   raised `AssertionError`, unlike a normal boot's replay, which tolerates
   skips) -- a commit must apply cleanly, not just avoid crashing.
3. **`registry_handlers_async`** -- every registered tool's `.handler` is
   still a coroutine function after replay (an overlay that swapped in a
   plain `def` instead of `async def` would otherwise fail silently later,
   at the first real call).
4. **`classify`** -- `repl.classify("x")` still returns a `Submission`
   (catches an overlay that redefined `fn:lictor.repl:classify` into
   something that no longer satisfies the REPL's contract).
5. **`build_options`** -- `options.build_options(cfg, state)` constructs a
   default `ClaudeAgentOptions` without raising.
6. **`servers`** -- `registry.servers()` builds exactly one MCP server per
   namespace (`sorted(servers) == sorted(namespaces)`).

A failing check is caught and recorded as `{"ok": false, "detail":
"<ExceptionType>: <message>"}` rather than propagated -- the probe always
emits exactly one JSON object on stdout, never a bare traceback, because
`overlay.run_probe` depends on being able to parse the last line of
output regardless of what went wrong inside.

## Why a schema change is refused

The Agent SDK fixes a tool's wire name and JSON schema at the moment
`create_sdk_mcp_server` is called (see `docs/architecture.md`, "why the
registry holds `SdkMcpTool` objects"); `ToolRegistry.servers()` builds
that server once and caches it, specifically so that a later `rebind`
is visible without rebuilding. A `redefine` whose new `@tool` declares a
different input schema cannot change what the client (Claude Code) thinks
the tool accepts -- only a fresh connection, with a freshly built server,
can do that. So `overlay.apply`'s `tool:` branch compares schemas
structurally (`overlay.schemas_match`, which resolves `TypedDict` type
hints rather than comparing class identity, so two independently-defined
but structurally identical schemas still match) and refuses outright on a
mismatch, with the message `"tool:{dotted}: a schema change needs a
restart"` -- rather than silently installing a handler the wire schema no
longer describes.

## The list of late-bound call sites a `fn:` redefine can safely replace

A `fn:<module>:<attr>` redefine works by `setattr`ing the module's own
attribute (`overlay.apply`, the `kind == "fn"` branch). That change is
only *visible* to code that looks the attribute up **again, through the
module object, at call time** -- `module.attr(...)`, or
`alias_for_module.attr(...)` after `from . import module as alias_for_module`.
It is invisible to any code that already holds its own bound reference
to the old function object, which is exactly what `from module import
attr` followed by a bare `attr(...)` produces: that name was resolved
once, at the `from ... import` statement's own execution time, and never
looked up again afterwards. This is not a lictor-specific trick, it is
just how Python name binding works -- but it means only *some* call sites
in this codebase are actually redefinable this way, and this list was
derived by reading every `from lictor... import` and `lictor.module.attr`
call site in the tree (`grep -rn` over `lictor/*.py` and
`lictor/tools/*.py`), not assumed from the module docstrings alone.

**Safe -- reached only through a module-qualified attribute lookup at call
time:**

- `fn:lictor.repl:classify` -- called as `repl.classify(...)` in
  `app.py` (`from . import repl`) and as `repl_mod.classify(...)` in
  `probe.py`.
- `fn:lictor.repl:eval_image` -- called as `repl.eval_image(...)` in
  both `app.py` and `image.py` (`from . import overlay, repl`).
- `fn:lictor.options:build_options` -- called as
  `options_mod.build_options(...)` in `brain.py`
  (`from . import options as options_mod`). `probe.py`'s own
  `build_options` check re-imports it fresh
  (`from .options import State, build_options`) *inside* the closure that
  runs after that same probe run's overlay replay, so it also observes a
  redefinition applied earlier in the same probe invocation -- but this
  is a one-off of the probe's own structure, not a general property of
  `from ... import`; see the "unsafe" list below for the normal case.
- `fn:lictor.overlay:active_generation` -- called as
  `overlay_mod.active_generation(...)` in `recovery.py`, `app.py`, and
  `__main__.py`, and as `overlay.active_generation(...)` in
  `tools/self_.py` (`from .. import overlay`).
- `fn:lictor.overlay:replay` -- called as `overlay_mod.replay(...)` in
  `__main__.py` and `probe.py`.
- `fn:lictor.recovery:decide_boot` / `:write_starting` / `:mark_booted` /
  `:clear` -- called as `recovery_mod.<name>(...)` in `__main__.py` and
  `app.py` (`from . import recovery as recovery_mod`).
- `fn:lictor.records:list_conversations` -- called as
  `records_mod.list_conversations(...)` in `app.py`
  (`from . import records as records_mod`).

**Not safe -- bound once by a direct `from module import name`, before any
overlay could apply, and never looked up again:**

- `fn:lictor.options:scrub_environ` as reached from `__main__.main`
  (`from .options import scrub_environ`, called immediately, before the
  overlay journal is even replayed) and from `workers.py`'s
  `_scrubbed_env()` (`from .options import scrub_environ` at module level,
  bound the first time `workers.py` itself is imported -- which, via
  `tools/py.py`, happens during `tools.load()`, itself before the replay
  step in `__main__.main`). A redefine of this target *would* still apply
  live in the process that proposed it (nothing stops `self.redefine`
  from rebinding `lictor.options.scrub_environ` the attribute), and it
  would still be picked up correctly on the next boot by anything on the
  "safe" list -- there just is no such caller for this particular
  function today, so a committed redefine of it would have zero effect on
  either of its two actual call sites. This is `[OPEN]`: it is a real gap
  between what `self.redefine` will let you target and what will actually
  change, not something this milestone closes.
- The one-shot `from .X import Y` imports inside `__main__.main` for
  `State`, `App`, `Brain`, `Approvals`, `Renderer`, and inside `brain.py`
  for `Submission` (type-checking only, `TYPE_CHECKING` guarded) are not
  meaningfully "late-bound call sites" at all in this sense -- each of
  those names is a class, constructed exactly once per process at
  startup, so there is no repeated call site for a redefine to retroactively
  change even in principle.

`tool:` targets are unaffected by any of this: the registry always holds
the live `SdkMcpTool` object itself, and the SDK reads `.handler` off that
same object fresh on every call (see `docs/architecture.md`) -- that
mechanism is late-bound by construction, with no `from ... import`
pitfall available to fall into.
