"""Bubblewrap (``bwrap``) sandboxing for the ``py.*`` workers only.

**Scope decision, stated here because this module is where it is enforced.**
lictor sandboxes exactly one class of subprocess: the ``py.*`` worker
children (``lictor/workers.py`` -> ``python -m lictor.worker_main``). It
does **not** sandbox ``exs.*`` (``lictor/tools/exs.py``), and that split is
deliberate, not an oversight:

- A ``py.*`` worker is where *untrusted, model-generated* code runs --
  ``docs/architecture.md``'s "boundary that matters" section already treats
  it as the thing that must not be able to reach back into lictor's own
  image or the host. Denying it network and host-write access is a
  strictly additional layer on top of the process-isolation it already
  has (own process group, scrubbed env, stdlib-only child).
- ``exs.*`` runs **this repository's own checked-in scripts** -- `make`,
  `tests/run.sh`, `trvthnvke verify` -- via ``nix develop -c``, and the
  entire point of those tools is that they behave *exactly* as they do for
  a human sitting at a terminal in `nix develop`. `nix develop` needs to
  talk to the Nix daemon over its UNIX socket (typically under
  `/nix/var/nix/daemon-socket/` or a per-user equivalent); a sandbox that
  changed how the gates resolve dependencies, find the toolchain, or see
  the filesystem would make the gates' PASS/FAIL a lie about what a human
  would get. So ``exs.*`` keeps running unsandboxed, the same way Claude
  Code's own built-in `Bash` tool keeps its own separate sandbox story
  that lictor does not touch or duplicate.
  **[OPEN]**: whether `nix develop -c ...` can be made to run correctly
  under `bwrap` at all is untested here. The blocking question is the Nix
  daemon socket: it would need to be explicitly `--bind`-mounted into the
  sandbox (its path varies by install -- single-user vs. multi-user Nix,
  and NixOS's own daemon socket location), and even then client/daemon
  protocol version skew or ambient ownership checks on the socket might
  behave differently from a bare shell. The untried alternative is to
  sidestep `nix develop` entirely for the sandboxed case: run
  `nix print-dev-env` once per session *outside* any sandbox to capture
  the resolved shell environment, then sandbox only the plain subprocess
  that uses that captured environment -- no daemon socket needed inside
  the sandbox at all, at the cost of a once-per-session unsandboxed Nix
  call and whatever determinism gap exists between "env captured once"
  and "`nix develop -c` fresh every time". Neither path has been tried;
  this stays [OPEN] rather than guessed at.

**The policy, when it does wrap** (see ``wrap`` below for the flag list and
the reason for each flag). Empirically confirmed on this machine
(bubblewrap 0.11.2) rather than assumed:

- ``--ro-bind / /`` leaves ``/nix/store`` readable -- confirmed by running
  ``python3 -c "import sys; print(sys.version)"`` under the exact flag set
  below; the interpreter (which lives in the store) starts and runs.
- A ``--bind <path> <path>`` writable hole works: a file written under it
  succeeds; a file written to an unbound path (``/etc/...``) fails with
  ``OSError: [Errno 30] Read-only file system``.
- ``--unshare-net`` makes `socket.create_connection` fail outright
  (``OSError: [Errno 101] Network is unreachable``), confirmed against a
  real address with a short timeout.
- ``os.killpg`` on the pgid asyncio hands back from
  ``start_new_session=True`` **still works** with ``--unshare-pid`` in the
  flag set, but only *because* ``--die-with-parent`` is also present --
  see ``lictor/workers.py``'s module docstring for the full empirical
  finding (bwrap forks an inner monitor process into a *second*, distinct
  process group that ``killpg`` on the outer group does not directly
  reach; without ``--die-with-parent`` that inner group is orphaned and
  keeps running after the outer `bwrap` is killed).

``--new-session`` deliberately detaches the child from the controlling
terminal (it is equivalent to calling ``setsid()`` a second time / dropping
any TIOCSCTTY association) -- correct for a worker, which must never be
able to read the operator's terminal stdin or receive a terminal-generated
signal meant for the foreground job, and this is also why ``wrap`` should
never be reached for for anything interactive: an interactive child *wants*
the controlling terminal, and ``--new-session`` would break it.
"""

from __future__ import annotations

import shutil
import warnings
from pathlib import Path
from typing import Any

#: Set the first time `wrap` warns about a missing `bwrap` in "auto" mode,
#: so the warning is emitted once per process rather than once per worker
#: spawn. Deliberately a plain module-level flag (not a lock/counter): the
#: only requirement is "don't spam the log", and this is read and written
#: only from the asyncio loop thread that owns worker spawning.
_warned_missing: bool = False


class SandboxUnavailable(RuntimeError):
    """Raised by `wrap` when `cfg.sandbox.bwrap == "require"` and no
    `bwrap` binary can be found on `PATH`."""


def available() -> str | None:
    """The absolute path to the `bwrap` binary, or `None` if it is not on
    `PATH`. A thin wrapper over `shutil.which` so every other function in
    this module (and every test) has exactly one place that answers
    "is bwrap here" -- and so a test can `monkeypatch` this one function
    to simulate bwrap's absence without touching `PATH` itself."""
    return shutil.which("bwrap")


def mode(cfg: Any) -> str:
    """The configured sandbox mode: `"auto"`, `"off"`, or `"require"`, read
    from `cfg.sandbox.bwrap` (see `lictor/config.py`'s `SandboxConfig`).

    `cfg=None` -- used by call sites and tests that have not wired up a
    `Config` at all -- is treated as the packaged default, `"auto"`,
    rather than an error: a worker spawned with no config present should
    behave exactly as a worker spawned with a freshly-constructed default
    `Config` would.
    """
    if cfg is None:
        return "auto"
    return cfg.sandbox.bwrap


def wrap(
    argv: list[str],
    *,
    writable: tuple[Any, ...] = (),
    network: bool = False,
    cfg: Any = None,
) -> list[str]:
    """Wrap `argv` in a `bwrap` invocation per `mode(cfg)`, or return it
    unchanged.

    - `mode == "off"`: `argv` is returned unchanged, no `bwrap` involved.
    - `mode == "auto"` and `bwrap` is absent: `argv` is returned unchanged,
      and a `RuntimeWarning` is emitted the first time this happens in the
      process (see `_warned_missing`) -- callers that want a stronger
      guarantee should configure `"require"` instead of inspecting the
      warning.
    - `mode == "require"` and `bwrap` is absent: raises `SandboxUnavailable`
      naming the `[sandbox] bwrap` config knob, so the operator knows
      exactly what to change (install `bubblewrap`, or relax the knob to
      `"auto"`/`"off"`).
    - Otherwise (`bwrap` present, mode `"auto"` or `"require"`): returns the
      full `bwrap` command line described below, followed by `--` and the
      original `argv`.

    `writable` is an iterable of paths (``str`` or ``Path``) that get a
    `--bind <path> <path>` hole punched through the otherwise-read-only
    bind of `/`; a path that does not exist on the host is skipped (`bwrap`
    itself errors on a `--bind` source that is missing, and a worker's
    `cwd` or a not-yet-created state directory is a normal case, not a
    sandbox-configuration bug).
    """
    m = mode(cfg)
    if m == "off":
        return list(argv)

    bwrap_path = available()
    if bwrap_path is None:
        if m == "require":
            raise SandboxUnavailable(
                "sandbox: [sandbox] bwrap = \"require\" but no `bwrap` binary is on PATH. "
                "Install bubblewrap, or set [sandbox] bwrap to \"auto\" or \"off\" in config.toml."
            )
        global _warned_missing
        if not _warned_missing:
            warnings.warn(
                "sandbox: [sandbox] bwrap = \"auto\" but no `bwrap` binary is on PATH; "
                "py.* workers are running WITHOUT a sandbox. Set [sandbox] bwrap = \"require\" "
                "to refuse instead of silently falling back.",
                RuntimeWarning,
                stacklevel=2,
            )
            _warned_missing = True
        return list(argv)

    flags: list[str] = [
        bwrap_path,
        # The whole host filesystem, read-only. This is what keeps
        # /nix/store (where the Python interpreter and lictor itself live
        # in a packaged install) readable without naming it specially --
        # confirmed empirically rather than assumed, see the module
        # docstring.
        "--ro-bind", "/", "/",
        # A fresh /dev rather than the host's live device nodes.
        "--dev", "/dev",
        # A fresh /proc for this sandbox's (eventually unshared) pid
        # namespace, not a view onto the host's real process table.
        "--proc", "/proc",
        # Private, writable scratch space at /tmp that is NOT the host's
        # /tmp -- a worker's own tempfile use should not collide with, or
        # be visible to, anything else on the machine.
        "--tmpfs", "/tmp",
    ]
    for path in writable:
        p = str(path)
        if not Path(p).exists():
            # bwrap errors on a --bind source that does not exist; a
            # worker cwd or a not-yet-created directory is routine, not a
            # misconfiguration, so it is skipped rather than failing wrap().
            continue
        # Punch a read-write hole through the read-only / bind above for
        # exactly this path -- the workspace and lictor's own state dir,
        # from the workers.py call site.
        flags += ["--bind", p, p]

    if not network:
        # The whole point of sandboxing py.*: a worker gets no socket
        # family at all, confirmed to raise OSError on a real connect.
        flags.append("--unshare-net")

    flags += [
        # Its own pid namespace -- the sandboxed command becomes pid 1
        # inside it rather than sharing the host's pid space. See
        # workers.py's docstring for what this does to os.killpg.
        "--unshare-pid",
        # Its own IPC namespace: no access to the host's SysV IPC /
        # POSIX message queues.
        "--unshare-ipc",
        # Its own UTS namespace: can't see or change the host's
        # hostname/domainname.
        "--unshare-uts",
        # The sandboxed command (and bwrap's own inner monitor process)
        # dies when bwrap's parent dies. This is also what makes
        # os.killpg on the *outer* bwrap process's pgid actually clean up
        # the whole sandboxed tree despite --unshare-pid putting the
        # sandboxed command in a different process group than the outer
        # bwrap invocation -- see the empirical note above and in
        # workers.py.
        "--die-with-parent",
        # Detach from the controlling terminal. Right for a worker (must
        # never read the operator's stdin or catch a terminal signal
        # meant for the foreground job); wrong for anything interactive.
        "--new-session",
        "--",
    ]
    return flags + list(argv)


def describe(cfg: Any) -> str:
    """One line describing the current sandbox posture, for `/status` and
    the startup banner."""
    m = mode(cfg)
    bwrap_path = available()
    if m == "off":
        return "sandbox: off -- py.* workers run unsandboxed"
    if bwrap_path is None:
        if m == "require":
            return 'sandbox: require, but no bwrap on PATH -- py.* workers refuse to start'
        return "sandbox: auto, no bwrap on PATH -- py.* workers run unsandboxed"
    return (
        f"sandbox: {m}, bwrap at {bwrap_path} -- py.* workers run with no network, "
        "a read-only host, and the workspace + lictor state dir writable"
    )
