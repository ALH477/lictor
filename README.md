# lictor

lictor is an autolith-style terminal agent for Exsecutor, driven by the user's own local Claude
Code binary rather than a direct Anthropic API key — it runs on the Claude Agent SDK, which
launches that binary as a subprocess, so inference rides the same subscription login `claude`
already uses on this machine.

## Evidence

- **M0 auth probe passed, 2026-10-01** (`scripts/m0_auth_probe.py`, run via `nix develop -c python scripts/m0_auth_probe.py` from inside a Claude Code session with the script's own scrub of `CLAUDE_CODE_*`, `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `ANTHROPIC_API_KEY`; no API key exists on the machine): `claude` 2.1.266 answered `pong`, `init: model=claude-opus-5[1m]`, `result subtype: success`, `is_error: False`, `num_turns: 1`. The reported `total_cost_usd: 0.00306` is Claude Code's client-side estimate, not a bill. SDK 0.2.163.
- **M1 one real turn, 2026-10-01**: `python -m lictor --once` streamed a prose turn and wrote `meta`, `prompt`, `assistant_text`, `result` to the conversation record. Interactive Ctrl-C is [UNTESTED]: there was no tty.
- **M2 exercised against the real Exsecutor tree, 2026-10-01**: `exs.spec` sliced §13 at lines 2869-3059 of `docs/spec/exsecutor-spec-v0.4.md`, and `exs.codes` reported `EXS-E0103` as "bidi or invisible control character in source" and present in `compiler/x86_64/diag/codes.inc`. In a live turn the model called `mcp__exs__codes` and answered from it.
- **M4 `py.*` against real children, 2026-10-01**: namespace persisted across evals, a `NameError` returned as data, `while True: pass` was killed by process group and the next eval restarted the worker, and the child environment carried no `CLAUDE_CODE_*` variables.
- **M4 `rlm.infer` against the real binary, 2026-10-01**: one tool-less frame over `CLAUDE.md` with a JSON schema returned `{"count": 9, "syscalls": ["read","write","close","fstat","lseek","mmap","munmap","openat","exit_group"]}`, the §9.3 allowlist, with the context hashed and one turn charged to the budget.
- **M4 vault crash survival, 2026-10-01**: a real child was `SIGKILL`ed between marking an item in flight and completing it; a fresh vault still reported the item.
- [UNTESTED] the `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` fallback path: not needed, not exercised.

## Terms

Anthropic's Agent SDK documentation states: "Unless previously approved, Anthropic does not allow
third party developers to offer claude.ai login or rate limits for their products, including agents
built on the Claude Agent SDK." lictor is a private tool run by its author on their own account; it
offers nothing to third parties and never calls the Anthropic API directly.
