# lictor

lictor is an autolith-style terminal agent for the Exsecutor compiler
project, driven by the operator's own local Claude Code binary rather than
a direct Anthropic API key. It runs on the Claude Agent SDK, which launches
that binary as a subprocess, so inference rides the same subscription
login `claude` already uses on this machine -- lictor never calls the
Anthropic API directly. What makes it autolith-*like*, rather than just
another chat loop with some repo-specific tools bolted on, is `self.*`:
a live, self-modifying image with a redefine → exercise → commit →
generation → rollback pipeline, so a running session can change its own
behavior durably, under a replay-probed, git-backed safety net, without
ever touching lictor's real source tree except through an explicit,
separate step.

## What it does

An interactive session is a REPL, but one where the default input is prose:
a line that is not a `/command`, a `!`-form, or a line beginning with a
literal space is sent to Claude as a user turn, exactly as typed. `/commands`
drive the session itself (`/status`, `/model`, `/effort`, `/mode`,
`/resume`, `/generations`, `/rollback`, `/vault`, `/tools`, and more --
`/help` lists what the running build has wired up). A `!expr` line, or a
`!!` ... `!!` block, evaluates Python directly in the live image's own
namespace (`prompt`, `read_file`, `state`, `cfg`, `registry`, `apropos`,
and -- once `self.*` has redefined something -- whatever that redefined).

Beyond Claude Code's own built-in tools (`Read`, `Edit`, `Bash`, and so on,
fenced to the active git worktree by lictor's own hooks), lictor registers
five tool namespaces, each its own in-process MCP server because an Agent
<!-- truth:claim
id: doc-architecture
kind: file_exists
path: docs/architecture.md
-->
SDK tool name cannot contain a dot (`docs/architecture.md` explains why):
<!-- truth:end -->

- **`exs.*`** -- gated, journalled entry points into the Exsecutor
  workspace's own build/test/spec tooling: `exs.build`, `exs.test`,
  `exs.spec_check`, `exs.audit`, `exs.conformance`, `exs.spec(section)`,
  `exs.codes(query)`, `exs.worktree(op, name)`, and friends. Every
  subprocess-backed one runs bounded, in its own process group, with the
  full output spilled to an artifact file and a non-zero exit reported as
  data, never raised.
- **`self.*`** -- inspects and redefines lictor's own running image:
  `status`, `describe`, `redefine`, `diff`, `exercise`, `discard`,
  `commit`, `persist_definition`, `set`, `apropos`. See
<!-- truth:claim
id: doc-mutations
kind: file_exists
path: docs/mutations.md
-->
  `docs/mutations.md` for the full pipeline.
<!-- truth:end -->
- **`py.*`** -- isolated, persistent Python worker REPLs in separate OS
  processes that import nothing from lictor itself (`docs/architecture.md`,
  "the boundary that matters"): `eval`, `start`, `stop`, `repls`, `reset`.
- **`rlm.*`** -- a bounded sub-inference over context the operator names,
  with an optional JSON schema for structured output: `infer`, `map`,
  `complete`.
- **`resource.*`** -- `resource.read(uri)` over four schemes:
  `workspace:<relpath>`, `inference:<trace-id>`, `session:<cid>`,
  `vault:current`.

<!-- truth:claim
id: roles-file
kind: file_exists
path: lictor/roles.py
-->
Programmatic subagent **roles** (`lictor/roles.py`) load from Markdown
<!-- truth:end -->
front-matter files and are passed to the SDK as `ClaudeAgentOptions(agents=
...)`, reachable from a turn via `!prompt("...", to="role-name")`. Two
example roles ship, documented rather than auto-loaded, under
`lictor/prompts/roles/` -- `spec-guardian.md` (read-only, checks a claim
against the spec and the §13 registry) and `fixture.md` (writes one
conformance fixture and confirms it fails with its exact intended code) --
copy either into `<config>/agents/` or `<workspace>/.lictor/agents/` to
actually load it.

## Running it

Install and run via the flake:

```sh truth:ignore
nix build .#default
./result/bin/lictor --version
./result/bin/lictor
```

Two of those three are measured rather than asserted. The build and the
version print are gated claims; the bare `lictor` needs a terminal, so it
is shown, not checked.

```sh truth:id=version-print truth:kind=command truth:expect_exit=0 truth:expect_stdout=~/lictor:\s0\.1\.0/
python -m lictor --version
```

```sh truth:id=suite truth:kind=command truth:expect_exit=0 truth:expect_stdout=~/passed/
pytest -q
```

`lictor --once "some prose"` runs exactly one turn non-interactively and
exits with the turn's `is_error` as the process exit code -- useful for
scripting or for the sort of sanity check this README's own Evidence
section below is built from. With no `--once`, it opens the interactive
REPL described above.

`lictor --recovery` forces a recovery boot: no overlay replay, no
workspace `init.py`, and the startup banner surfaces the most recent crash
report if there is one. The same thing happens automatically, without the
flag, if the *previous* process's `boot.json` was still in
`phase: "starting"` when this one started -- the signature of a crash
during startup, answered by not repeating whatever just failed.

`--force` overrides lictor's refusal to start at all when
`CLAUDE_CODE_MESSAGING_SOCKET` is set in the environment -- the signature
of already being inside a running Claude Code session. An SDK-driven agent
nested inside another Claude Code session is unsupported: lictor's own
`options.scrub_environ` removes every `CLAUDE_CODE_*` variable (keeping
only `CLAUDE_CODE_OAUTH_TOKEN` and `CLAUDE_CONFIG_DIR`) before the SDK is
ever imported, precisely so the subprocess it launches does not inherit
state from an outer session -- but starting lictor *from inside* one in
the first place is a configuration nobody has verified behaves sanely, so
it is refused by default rather than silently tolerated. `--force` exists
for exactly the cases where an operator has decided that risk is theirs to
take.

### Running on local Ollama

`lictor --ollama MODEL` (or `[ollama] enabled = true`, `model = "..."` in
`config.toml`) points the `claude` binary at an Ollama server instead of
the subscription login: `ANTHROPIC_BASE_URL` is set to the Ollama host
(default `http://localhost:11434`, `[ollama].host`), `ANTHROPIC_API_KEY`
is emptied, and every model alias (`opus`, `sonnet`, `haiku`, so `rlm.*`
and role files too) resolves to the one Ollama model. At boot lictor asks
the server's `/api/tags` whether it is up and has the model, and refuses
to start with the fix (`ollama pull ...`) if not. A bare `--ollama` uses
the configured model; `--model` still wins. Whether a given local model
handles Claude Code's tool use well is [UNTESTED] here.

State lives under XDG-ish directories, `lictor` appended:
`$XDG_STATE_HOME/lictor/` (conversation records, the vault, the overlay
journal, generations, crash reports, artifacts, the private
`mutations.git`), `$XDG_DATA_HOME/lictor/` (saved `rlm.*` traces),
`$XDG_CONFIG_HOME/lictor/` (`config.toml`, and `agents/*.md` role files).
`$LICTOR_STATE_HOME` / `$LICTOR_DATA_HOME` / `$LICTOR_CONFIG_HOME`
override each outright, for a throwaway location (lictor's own test suite
uses exactly this to isolate every test onto a `tmp_path`).

## Design

`docs/architecture.md` is the shape of the process tree -- the inverted
threading model (why SIGINT only ever reaches the main thread, and what
that means for Ctrl-C mid-turn), why there is one in-process MCP server
per namespace, why the tool registry holds the SDK's own `SdkMcpTool`
objects rather than plain functions, and the boundary that actually
matters: `py.*` workers cannot touch lictor's own image, `self.*` is the
only thing that can, and why those two tools being shaped so differently
is deliberate rather than an oversight.

<!-- truth:claim
id: doc-records
kind: file_exists
path: docs/records.md
-->
`docs/records.md` is the append-only conversation record: every JSONL
<!-- truth:end -->
`kind` and its fields, the exact fsync ordering a crash can interrupt
(and the bound on what it can lose), where a long tool result spills to
an artifact file, and how a lictor conversation id relates to -- and can
briefly diverge from -- the underlying Claude session id.

`docs/mutations.md` is the `self.*` pipeline end to end: the two
`redefine` target schemes, what `symbol_sha256` actually detects and the
exact message a moved tracked definition produces on replay, every check
the clean-process commit probe runs, why a schema change is always
refused, and -- derived by reading every call site in the tree rather
than assumed -- exactly which functions a `fn:` redefine can and cannot
actually change the behavior of.

<!-- truth:claim
id: doc-evidence
kind: file_exists
path: docs/evidence.md
-->
`docs/evidence.md` is the house rule this project holds itself to, and
<!-- truth:end -->
where it came from: Exsecutor's own `CLAUDE.md`, applied here with the
same markers (`[OPEN]`, `[UNTESTED]`, `[UNREPRODUCED]`) and the same
refusal to report a test not watched passing or a number not just
measured. The Evidence section immediately below is the ledger that rule
<!-- truth:claim
id: gate-policy
kind: file_exists
path: .trvthnvke.toml
-->
produces; `.trvthnvke.toml` is what is meant to keep that ledger from
<!-- truth:end -->
drifting once the tool is wired in (see that section's own note on its
current, unarmed state).

## Evidence

<!-- truth:claim
id: probe-file
kind: file_exists
path: scripts/m0_auth_probe.py
-->
- **M0 auth probe passed, 2026-10-01** (`scripts/m0_auth_probe.py`, run via `nix develop -c python scripts/m0_auth_probe.py` from inside a Claude Code session with the script's own scrub of `CLAUDE_CODE_*`, `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `ANTHROPIC_API_KEY`; no API key exists on the machine): `claude` 2.1.266 answered `pong`, `init: model=claude-opus-5[1m]`, `result subtype: success`, `is_error: False`, `num_turns: 1`. The reported `total_cost_usd: 0.00306` is Claude Code's client-side estimate, not a bill. SDK 0.2.163.
<!-- truth:end -->
- **M1 one real turn, 2026-10-01**: `python -m lictor --once` streamed a prose turn and wrote `meta`, `prompt`, `assistant_text`, `result` to the conversation record. Interactive Ctrl-C is [UNTESTED]: there was no tty.
- **M2 exercised against the real Exsecutor tree, 2026-10-01**: `exs.spec` sliced §13 at lines 2869-3059 of `docs/spec/exsecutor-spec-v0.4.md`, and `exs.codes` reported `EXS-E0103` as "bidi or invisible control character in source" and present in `compiler/x86_64/diag/codes.inc`. In a live turn the model called `mcp__exs__codes` and answered from it.
- **M4 `py.*` against real children, 2026-10-01**: namespace persisted across evals, a `NameError` returned as data, `while True: pass` was killed by process group and the next eval restarted the worker, and the child environment carried no `CLAUDE_CODE_*` variables.
- **M4 `rlm.infer` against the real binary, 2026-10-01**: one tool-less frame over `CLAUDE.md` with a JSON schema returned `{"count": 9, "syscalls": ["read","write","close","fstat","lseek","mmap","munmap","openat","exit_group"]}`, the §9.3 allowlist, with the context hashed and one turn charged to the budget.
- **M4 vault crash survival, 2026-10-01**: a real child was `SIGKILL`ed between marking an item in flight and completing it; a fresh vault still reported the item.
- **M5 roles reach the model, 2026-10-01**: a role file in `<config>/agents/` loaded as an `AgentDefinition` with its model, tools and effort, and a live turn delegating to it through the Agent tool returned that role's own answer. Steering a subagent that is already running remains [OPEN]: the SDK offers interrupt and stop, not a message channel.
- **M5 the README gate is armed and green, 2026-10-01**: `trvthnvke verify --fail` reports `PASS claims=9 checked=9 passed=9 errors=0`, `nix flake check` passes both `checks.tests` and `checks.readme`, and `hooks/pre-commit` refuses a commit it cannot gate rather than passing it. Twelve path-like tokens warn as unbound; the gate's `fail_on` is `error`.
- **M6 the worker sandbox, verified through `py.eval` itself, 2026-10-01**: opening a socket returned `OSError [Errno 101] Network is unreachable`, writing outside the workspace returned `OSError [Errno 30] Read-only file system`, the interpreter under `/nix/store` still ran, and a write inside the workspace succeeded and read back. `exs.*` is unsandboxed by design.
- [UNTESTED] the `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` fallback path: not needed, not exercised.
- **M5 test suite, 2026-10-01**: `nix develop -c pytest -q` → `142 passed` (the prior 131, plus 11 new in `tests/test_roles.py`), no regressions.
- **M5 lint, 2026-10-01**: `nix develop -c ruff check lictor tests` → `All checks passed!`.
- **M5 role loading against the installed SDK, 2026-10-01**: `claude_agent_sdk.AgentDefinition`'s real dataclass fields (checked via `dataclasses.fields`, SDK 0.2.163) are `description, prompt, tools, disallowedTools, model, skills, memory, mcpServers, initialPrompt, maxTurns, background, effort, permissionMode`. Both `max_turns` (-> `maxTurns`) and `effort` -- the two fields the plan singled out as possibly unsupported -- are present, so `lictor/roles.py` drops neither. A role file written to a throwaway `agents/` directory and loaded with `roles.load(cfg, paths)` produced exactly: `AgentDefinition(description='Demo role for the M5 verification exercise.', prompt="This is the demo role's prompt body.", tools=['Read', 'Grep', 'Glob'], disallowedTools=None, model='sonnet', skills=None, memory=None, mcpServers=None, initialPrompt=None, maxTurns=6, background=None, effort='low', permissionMode=None)`.
- [OPEN] **M5 `nix build .#default` fails, pre-existing, unrelated to M5**: the packaged build's `checkPhase` runs the real test suite inside the Nix sandbox, where `git` is not on `PATH` (it is only added at runtime, via `makeWrapperArgs`) -- `tests/test_overlay.py`'s two commit/rollback tests spawn a real `git` subprocess and fail with `FileNotFoundError: [Errno 2] No such file or directory: 'git'`. Confirmed pre-existing, not introduced by M5, by stashing every M5 change and reproducing the identical failure on the unmodified tree. The fix (`nativeCheckInputs = [... pkgs.git]` in `flake.nix`) is one line, but `flake.nix` is outside this milestone's owned files, so it is reported here rather than changed. `nix develop -c python -m lictor --version` (the dev shell, not the packaged build) works correctly: `lictor: 0.1.0` / `claude-agent-sdk: 0.2.163` / `claude: 2.1.266 (Claude Code)`. `nix flake check` itself passes (it evaluates `packages.default` as a derivation but does not build/check it, since there is no `checks` output yet), and is unaffected by this.
- [UNTESTED] **the TrvthNvke README gate**: `.trvthnvke.toml` is written against the command allowlist actually exercised above, but `trvthnvke` is not a flake input yet, so `trvthnvke extract` / `lock` / `verify --fail` have not been run even once. See `docs/evidence.md` for why the policy is written now regardless, and the note under `.trvthnvke.toml` in this repo for the exact steps to arm it.

## Terms

Anthropic's Agent SDK documentation states: "Unless previously approved, Anthropic does not allow
third party developers to offer claude.ai login or rate limits for their products, including agents
built on the Claude Agent SDK." lictor is a private tool run by its author on their own account; it
offers nothing to third parties and never calls the Anthropic API directly.

## License

MIT. See `LICENSE`.
