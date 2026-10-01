# lictor

lictor is an autolith-style terminal agent for Exsecutor, driven by the user's own local Claude
Code binary rather than a direct Anthropic API key — it runs on the Claude Agent SDK, which
launches that binary as a subprocess, so inference rides the same subscription login `claude`
already uses on this machine.

## Evidence

- **M0 auth probe passed, 2026-10-01** (`scripts/m0_auth_probe.py`, run via `nix develop -c python scripts/m0_auth_probe.py` from inside a Claude Code session with the script's own scrub of `CLAUDE_CODE_*`, `CLAUDECODE`, `CLAUDE_PID`, `CLAUDE_EFFORT`, `ANTHROPIC_API_KEY`; no API key exists on the machine): `claude` 2.1.266 answered `pong`, `init: model=claude-opus-5[1m]`, `result subtype: success`, `is_error: False`, `num_turns: 1`. The reported `total_cost_usd: 0.00306` is Claude Code's client-side estimate, not a bill. SDK 0.2.163.
- [UNTESTED] the `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` fallback path: not needed, not exercised.

## Terms

Anthropic's Agent SDK documentation states: "Unless previously approved, Anthropic does not allow
third party developers to offer claude.ai login or rate limits for their products, including agents
built on the Claude Agent SDK." lictor is a private tool run by its author on their own account; it
offers nothing to third parties and never calls the Anthropic API directly.
