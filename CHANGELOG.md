# Changelog

## 0.1.0 (unreleased)

Built on 2026-10-01 in six milestones. Each milestone's own commit message
carries its evidence; `README.md`'s Evidence section is the ledger.

| milestone | what it added | commit |
|---|---|---|
| M0 | repo skeleton, the flake with SDK 0.2.163, the auth probe | `2498b40` |
| — | the tool registry seam, one MCP server per namespace | `6a1b9e2` |
| M1 | the REPL, the brain, append-only records, approvals | `4e31e30` |
| M2 | the `exs.*` tools, the edit and shell fences | `b0d5bcd` |
| M4 | `py.*` worker REPLs, `rlm.*` bounded inference, the vault | `1ee3e6a` |
| — | registry wiring; the prompt stopped claiming tools were absent | `62f3aa4` |
| M3 | the live image, overlay generations, recovery | `75b30ce` |
| — | wiring the image into the launcher and the REPL | `140a42c` |
| M5 | child roles, the design docs, the gate policy | `ac5deab` |
| — | arming the README gate | `f913580`, `857c802` |
| — | loading the roles | `653342d` |
| M6 | the bubblewrap sandbox for `py.*` workers | see below |

### M6 has no commit of its own, and that is a mistake worth naming

The milestones were implemented by concurrent agents in one shared checkout.
While M6 was still being written, two commits of mine used `git add -A` and
swept its in-progress files in under unrelated messages:

- `f913580` ("arm the README gate") also carries `lictor/sandbox.py` and the
  `lictor/workers.py` change.
- `653342d` ("load the roles") also carries `tests/test_sandbox.py` and the
  `docs/architecture.md` sandbox section.

The tree is correct and the work is complete; only the history mis-attributes
it. The history is not rewritten, because these commits are published and a
correction that erases the error is worse than one that records it. The fix
for next time is the rule, not the cleanup: stage explicit paths, never
`git add -A`, while another agent is writing in the same checkout.

### Known limits at 0.1.0

- Interactive Ctrl-C, both mid-turn and at an empty prompt, is `[UNTESTED]`:
  no session that built this had a terminal.
- Steering a subagent that is already running is `[OPEN]`. The SDK offers
  interrupt and stop, not a message channel.
- `exs.*` runs unsandboxed by design; see `docs/architecture.md`.
- `self.redefine` can target `fn:lictor.options:scrub_environ`, which has no
  late-bound caller, so committing such a redefine would change nothing
  `[OPEN]`.
- Typing a follow-up while a turn is running is not possible; the vault holds
  submissions that did not complete, not queued steering.
