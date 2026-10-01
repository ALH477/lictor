# The conversation record

Every lictor conversation writes one append-only JSONL file:
`$LICTOR_STATE_HOME/lictor/conversations/<cid>.jsonl`, one JSON object per
line (`lictor/records.py`, class `Records`). This is the thing `/sessions`
lists, `resource.read("session:<cid>")` summarizes, and a crash-recovery
boot reads nothing from at all (recovery is driven by `boot.json` and
`crash/*.json` instead -- see "A lictor conversation id vs. a Claude
session id" below, and `docs/architecture.md` for where those live).

## Schema

One line is one record:

```json
{"seq": 7, "t": "2026-10-01T12:00:00.123Z", "cid": "<uuid4>", "kind": "...", "data": {...}}
```

- `seq` -- a 1-based counter, per conversation file, assigned by
  `Records.append`. A resumed conversation re-opens the same file and
  `Records.__post_init__` scans it once to resume the counter after the
  highest `seq` already on disk, rather than restarting at 1 and colliding.
- `t` -- UTC, millisecond precision, `Z` suffix (`records._now_iso`).
- `cid` -- the lictor conversation id this line belongs to (every line in
  one file carries the same value; see below for how it relates to the
  underlying Claude session id).
- `kind` -- one of the kinds below.
- `data` -- kind-specific fields.

## Every `kind` and its `data` fields

These are the kinds actually written by the code as it stands (not every
kind named in the original plan has a writer yet; this list is what
`grep -rn 'records.append(' lictor/` turns up, not an aspiration):

| kind | written by | `data` fields |
|---|---|---|
| `meta` | `__main__.main` | `lictor`, `sdk`, `claude` (version strings), `cwd`, `worktree`, `model`, `effort`, `generation`, `recovery` |
| `input` | `app.App.run` | `text` -- the raw line exactly as typed, before classification |
| `prompt` | `brain.Brain.submit` | `text`, `to`, `source` (the `Submission` about to be sent to Claude) |
| `assistant_text` | `brain.Brain.submit` | `text` |
| `tool_use` | `brain.Brain.submit` | `id`, `name`, `input` |
| `tool_result` | `brain.Brain._record_tool_result` | `tool_use_id`, `is_error`, `bytes`, `summary` (first `ARTIFACT_INLINE_LIMIT` = 2000 chars), `artifact` (present only if truncated -- see "Where artifacts spill") |
| `thinking` | `brain.Brain.submit` | `chars` -- the length only, never the thinking text itself |
| `result` | `brain.Brain.submit` | `num_turns`, `usage`, `total_cost_usd`, `terminal_reason`, `session_id`, `is_error`, `subtype` |
| `interrupt` | `brain.Brain.interrupt` | `{}` |
| `command` | `app.App._dispatch_command` | `name`, `args` |
| `image` | `app.App._handle_image` | `src` -- the `!`-form source evaluated |
| `error` | `app.App._handle_image`, `hooks.py` callbacks | `where`, `message` |
| `mutation` | `overlay.py` (`redefine`, `record_exercise`, `discard`, `replay`, `commit`, `rollback`) | varies by `action`: `redefine`/`exercise`/`discard`/`commit` carry `seq`; `commit` adds `gen`, `commit` (the git sha); `replay` carries `applied` and `skipped`; `rollback` carries `gen` |
| `hook_tool_result` | `hooks.build_hooks`'s `record_tool_result` | `tool_name`, `tool_use_id`, `length` (bytes/chars of the tool response, not its content) |

Two kinds the plan named but no code path writes yet: `vault` (the vault
has its own file, `vault/<cid>.json`, documented below -- nothing in
`vault.py` appends a conversation-record line) and `crash` (crash capture
writes `crash/<ts>.json`, keyed by timestamp, not folded into any
conversation's JSONL either). This is `[OPEN]`, not a bug to fix without
asking: both pieces of state already exist and are readable
(`/vault`, `resource.read("vault:current")`, the crash banner on a
recovery boot), they are just not *also* mirrored into the per-conversation
record.

## Crash-safety: fsync ordering per submission

`Records.append` is the unit of durability: it writes the line, then
`flush()` then `os.fsync(fh.fileno())`, **before returning** -- so a crash
can lose at most the one record that was being written when it happened,
never a record that `append()` already returned from. `brain.Brain.submit`
puts that guarantee to work in a specific order:

1. `prompt` is appended (fsynced) -- recording what is about to be sent,
   before it is sent.
2. `await self.client.query(sub.text)` -- the turn starts.
3. Every message the turn produces is appended as it arrives
   (`assistant_text`, `tool_use`, `tool_result`, `thinking`), each fsynced
   in turn.
4. `result` is appended once the turn ends.

The **raw input line** is written first, ahead of all of this, but by a
different writer: `app.App.run` appends `input` the moment `input()`
returns, before `repl.classify` has even looked at the line -- so the
record shows exactly what the operator typed even if classification,
image evaluation, or submission never happens (a bad `/command`, a
`!`-form that raises, Ctrl-C before anything is submitted). The ordering
promise end to end is: **`input` is durable before classification ever
runs; `prompt` is durable before the turn is sent; every streamed record
is durable before the next one is appended.** Nothing here is batched or
buffered across these fsync points -- the cost of a crash is bounded by
one write, by construction, not by a flush interval.

(The vault, `lictor/vault.py`, backs the same guarantee for the
*submission* itself -- not what the conversation record captured, but
whether an item ever reached `client.query()` at all -- with its own
atomic-rewrite file; see `docs/mutations.md`'s sibling discussion of the
overlay manifest for the general pattern, and the module's own docstring
for the vault's specific lifecycle.)

## Where artifacts spill

A tool result longer than `brain.ARTIFACT_INLINE_LIMIT` (2000 characters)
is not truncated and dropped -- the full text is written to
`$LICTOR_STATE_HOME/lictor/artifacts/<cid>/<seq>-<tool>.log`
(`Records.artifact_path`, called from `Brain._record_tool_result`), and
the conversation record's `tool_result` line carries only the first 2000
characters as `summary` plus the artifact's path as `artifact`. The
filename's `<seq>` is the *conversation record's* next sequence number
(`Records.next_seq`, read before the artifact is written so the filename
can name the record that is about to reference it), not a count of
artifacts; `<tool>` is the tool name with anything outside
`[A-Za-z0-9_.-]` replaced by `_` (`records._SAFE_CHARS`). `tools/exs.py`'s
own subprocess runner spills its *entire* combined stdout+stderr the same
way, for every run regardless of length, by calling `Records.artifact_path`
directly rather than going through `Brain._record_tool_result`'s
length check -- an `exs.*` run's full log is always on disk, even when
the tail shown inline is short.

## A lictor conversation id vs. a Claude session id

These are two different identifiers that usually agree, and the one place
they can diverge is worth being precise about. `state.cid` is lictor's own
id: a `uuid4` minted once in `__main__.main` for a fresh conversation (or
the value passed to `--resume`), and it is what names the `.jsonl` file,
the vault file, and the artifacts directory. `state.claude_session` is the
id the *Claude Code binary itself* assigned to the underlying session,
read out of the `SystemMessage("init")`'s `session_id` field the first
time `Brain.submit` sees one (`brain.py`: `self.state.claude_session =
msg.data.get("session_id")`).

On a **fresh** conversation, `options.build_options` passes `session_id=
fresh_cid` (the installed SDK's own field doing what the plan called
`extra_args={"session-id": cid}` -- see the comment in `options.py` for why
the direct field was used instead), pinning the two ids to the same value
from turn one: lictor picks the id, and asks Claude Code to use it too. On
a **resumed** conversation (`--resume CID`, or `/resume CID`, or a
`Brain.reconnect(claude_session=...)` from `/effort` or
`exs.worktree(op="new")`), `build_options` passes `resume=<that id>`
instead, and `fresh_cid`/`session_id` are not set -- `resume` and
`fresh_cid` are mutually exclusive by construction
(`build_options` raises `ValueError` if both are given). So in the common
case `state.cid == state.claude_session` throughout a conversation's life;
the two are tracked as separate fields, rather than lictor simply reusing
`state.claude_session` as its own conversation id, because the Claude
session id is not known until the first `init` message arrives, while
lictor needs a conversation id immediately -- to open the `.jsonl` file, the
vault, and the records directory before the first `client.connect()` call
is even made.
