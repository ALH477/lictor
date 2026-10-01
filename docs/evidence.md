# Evidence discipline

lictor inherits its evidence rule from the project it was built to work
on, verbatim: Exsecutor's `CLAUDE.md` puts it as

> Prose designs are hypotheses until code runs.

That line is binding here too, for the same reason it is binding there --
lictor is a tool whose whole purpose is letting an operator trust what an
agent reports about a codebase it is allowed to touch, including (through
`self.*`) its own codebase. A tool that is loose about its own evidence has
no business enforcing rigor on anyone else's.

## The rule, in practice

- **A claim backed by nothing gets `[OPEN]` or `[UNTESTED]`.** `[OPEN]` is
  for something genuinely undecided or not yet attempted (a design gap, a
  known limitation nobody has worked around yet). `[UNTESTED]` is for
  something that was implemented but has not actually been run and
  watched -- the distinction matters, because "I wrote the code for this"
  and "I watched this work" are different claims, and conflating them is
  exactly the failure mode this rule exists to prevent.
- **A figure that cannot currently be re-measured gets `[UNREPRODUCED]`.**
  A number that was true once, measured honestly, but cannot be checked
  again right now (the binary that produced it is gone, the environment
  changed, the measurement was expensive and one-shot) does not get
  silently repeated as current fact. It gets reported as what it is: a
  past result, not a present one.
- **Never present a re-derivation as a restoration.** Working out what a
  number *should* be from the code, the spec, or first principles is a
  legitimate thing to do -- but it is not the same act as re-running the
  original measurement, and reporting it as though it were ("confirmed:
  the figure is X" when X was computed rather than measured) manufactures
  confidence that was never earned.
- **Never report a benchmark you did not run, or a test you did not see
  pass.** Not "the tests should pass," not "this looks correct." A claim
  in lictor's own evidence trail means someone -- the operator, or an
  agent acting through lictor -- ran the thing and watched the actual
  output, in this session, not a prior one being taken on faith.

This is also, not coincidentally, what the `exs.*` tools are built to make
cheap to honor rather than cheap to skip: every `exs.*` subprocess run
(`lictor/tools/exs.py`) is journalled with its full combined output spilled
to an artifact file (`docs/records.md`, "Where artifacts spill") and a
non-zero exit reported back as data rather than swallowed -- so "I ran it
and it failed" is exactly as easy to report as "I ran it and it passed,"
and neither one requires trusting memory over the transcript.

## The ledger and the gate

**The README's `## Evidence` section is the ledger.** Every line in it
names, in the same breath, what was run and what came back -- the command
(or the mechanism, for something that is not a shell command: "a live turn
called `mcp__exs__codes`"), the environment it ran in when that is not
obvious, and the literal output, not a paraphrase of it. A line that
cannot meet that bar does not belong in Evidence; it either gets measured
first, or it gets written as prose elsewhere in the README with whichever
of the three markers above actually applies.

**TrvthNvke is what keeps that ledger honest once it is written down.**
A per-session discipline is only as good as everyone's memory of it; a
written claim that nobody re-checks drifts the moment the code underneath
it changes. `.trvthnvke.toml` (see `docs/architecture.md` and
`CONTRIBUTING.md`-style conventions in the Exsecutor tree this pattern is
adapted from) binds specific sentences in `README.md` to specific
commands from a closed allowlist, so that a claim is either re-verified
against the exact thing it asserts, or the gate fails the commit. The gate
itself is `[UNTESTED]` in this repository as of this milestone -- the
`trvthnvke` binary is not an input to lictor's flake yet -- but the policy
file is written now, against the real allowlist of commands that actually
work here, specifically so that arming it later (`nix develop`,
`trvthnvke extract`, `trvthnvke lock`, `trvthnvke verify --fail`) is a
matter of adding the tool as a flake input and running those four
commands, not a matter of writing the policy for the first time under
pressure once the Evidence section has already drifted.
