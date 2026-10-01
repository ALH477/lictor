"""The ``exs`` namespace: lictor's gated, recorded entry points into the
Exsecutor build/test/spec tooling.

Every subprocess-shaped tool here goes through the private ``_run`` runner:
a bounded ``asyncio.create_subprocess_exec`` (never ``shell=True``) in its
own process group, so a timeout can kill the whole group rather than
leaving orphaned children behind. Output is combined stdout+stderr,
spilled in full to an artifact file under the conversation's records
directory, and truncated to the last 200 lines / 32 KiB inline in the
tool result. A non-zero exit is reported back to the model (``isError``
is set on the MCP result) -- it is never raised as a Python exception,
because a failing build or test run is an ordinary, expected outcome the
model needs to see and reason about, not a bug in lictor.

``exs.spec`` and ``exs.codes`` are the two exceptions: they run no
subprocess at all, just read files out of the Exsecutor workspace.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
from pathlib import Path
from typing import Any

from claude_agent_sdk import tool

NAMESPACE = "exs"

#: Prefix every toolchain-dependent command runs behind (CLAUDE.md: "the
#: toolchain only exists inside `nix develop`").
_NIX_PREFIX = ("nix", "develop", "-c")

#: Output caps shared by every subprocess-backed tool.
_TAIL_MAX_LINES = 200
_TAIL_MAX_BYTES = 32 * 1024
_SPEC_MAX_BYTES = 60 * 1024

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")

_BASH_DEVICE_CHOICES = ("amdgcn",)


# ---------------------------------------------------------------------------
# the shared runner
# ---------------------------------------------------------------------------


def _tail(text: str, *, max_lines: int = _TAIL_MAX_LINES, max_bytes: int = _TAIL_MAX_BYTES) -> str:
    data = text.encode("utf-8", errors="replace")
    if len(data) > max_bytes:
        text = data[-max_bytes:].decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\n".join(lines)


async def _run(
    ctx: Any,
    argv: list[str],
    *,
    tool: str,
    timeout: float | None = None,
    nix: bool = True,
    cwd: Any = None,
) -> dict[str, Any]:
    """Run `argv`, bounded, recorded, never raising.

    Returns a dict with ``exit`` (the process return code, or ``None`` if
    it could not be determined), ``timed_out``, ``timeout`` (the bound
    actually used), ``argv`` (the full command line, nix prefix included),
    ``artifact`` (path to the full combined output, as ``str``), and
    ``tail`` (the last 200 lines / 32 KiB of that output).
    """
    full_argv = [*_NIX_PREFIX, *argv] if nix else list(argv)
    run_cwd = Path(cwd) if cwd is not None else Path(ctx.state.cwd)
    run_timeout = ctx.cfg.exs.timeout_s if timeout is None else timeout

    seq = ctx.records.next_seq
    artifact = ctx.records.artifact_path(seq, tool)

    timed_out = False
    exit_code: int | None = None
    output = b""

    try:
        proc = await asyncio.create_subprocess_exec(
            *full_argv,
            cwd=str(run_cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        output = f"failed to start {full_argv!r}: {exc!r}\n".encode()
        exit_code = -1
        artifact.write_bytes(output)
        ctx.records.append(
            "exs_run",
            {
                "tool": tool,
                "argv": full_argv,
                "exit": exit_code,
                "timed_out": False,
                "artifact": str(artifact),
                "bytes": len(output),
            },
        )
        return {
            "exit": exit_code,
            "timed_out": False,
            "timeout": run_timeout,
            "argv": full_argv,
            "artifact": str(artifact),
            "tail": _tail(output.decode("utf-8", errors="replace")),
        }

    try:
        output = await asyncio.wait_for(proc.stdout.read(), timeout=run_timeout)
        exit_code = await proc.wait()
    except TimeoutError:
        timed_out = True
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            exit_code = await asyncio.wait_for(proc.wait(), timeout=10)
        except TimeoutError:
            exit_code = None
        try:
            output = await asyncio.wait_for(proc.stdout.read(), timeout=10)
        except TimeoutError:
            output = output or b""

    artifact.write_bytes(output)
    ctx.records.append(
        "exs_run",
        {
            "tool": tool,
            "argv": full_argv,
            "exit": exit_code,
            "timed_out": timed_out,
            "artifact": str(artifact),
            "bytes": len(output),
        },
    )
    return {
        "exit": exit_code,
        "timed_out": timed_out,
        "timeout": run_timeout,
        "argv": full_argv,
        "artifact": str(artifact),
        "tail": _tail(output.decode("utf-8", errors="replace")),
    }


def _is_failure(res: dict[str, Any]) -> bool:
    return bool(res["timed_out"]) or res["exit"] not in (0,)


def _head_line(res: dict[str, Any]) -> str:
    if res["timed_out"]:
        return f"exit=timeout after {res['timeout']}s"
    return f"exit={res['exit']}"


def _format(res: dict[str, Any], *, extra: str | None = None) -> dict[str, Any]:
    parts = [_head_line(res), res["artifact"]]
    if extra:
        parts.append(extra)
    parts.append(res["tail"])
    text = "\n".join(parts)
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if _is_failure(res):
        out["isError"] = True
    return out


def _error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


# ---------------------------------------------------------------------------
# make-backed tools
# ---------------------------------------------------------------------------


def _make_tool(ctx: Any, name: str, target: str, note: str = "") -> Any:
    desc = (
        f"Run `make {target}` for the Exsecutor workspace, under `nix develop -c`. {note}"
        "Output is combined stdout+stderr, truncated to the last 200 lines / 32 KiB inline; "
        "the full log is written to the artifact path given in the result. A non-zero exit "
        "is reported in the result (isError is set), never raised."
    ).strip()

    @tool(name, desc, {"type": "object", "properties": {}})
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        res = await _run(ctx, ["make", target], tool=name)
        return _format(res)

    return handler


def _build_tool(ctx: Any) -> Any:
    return _make_tool(ctx, "build", "all")


def _audit_tool(ctx: Any) -> Any:
    return _make_tool(
        ctx,
        "audit",
        "audit",
        "This is the purity-contract check (CLAUDE.md §9.3): it fails the build if a "
        "socket-family syscall appears anywhere. ",
    )


def _reproduce_tool(ctx: Any) -> Any:
    return _make_tool(ctx, "reproduce", "reproduce")


def _check_tool(ctx: Any) -> Any:
    return _make_tool(ctx, "check", "check", "Runs without needing a compiler. ")


def _smoke_tool(ctx: Any) -> Any:
    return _make_tool(ctx, "smoke", "smoke")


def _test_tool(ctx: Any) -> Any:
    desc = (
        "Run `tests/run.sh` for the Exsecutor workspace, under `nix develop -c`. Optionally "
        "pass `device=\"amdgcn\"` to append `--device=amdgcn`; any other device value is "
        "refused before anything runs. Output is truncated to the last 200 lines / 32 KiB "
        "inline; the full log is written to the artifact path in the result. A non-zero exit "
        "is reported in the result (isError is set), never raised."
    )
    schema = {
        "type": "object",
        "properties": {"device": {"type": "string", "description": "only \"amdgcn\" is accepted"}},
        "required": [],
    }

    @tool("test", desc, schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        device = args.get("device")
        argv = ["tests/run.sh"]
        if device is not None:
            if device not in _BASH_DEVICE_CHOICES:
                return _error(
                    f"test: device {device!r} is not accepted (only {_BASH_DEVICE_CHOICES!r})"
                )
            argv.append(f"--device={device}")
        res = await _run(ctx, argv, tool="test")
        return _format(res)

    return handler


def _spec_check_tool(ctx: Any) -> Any:
    desc = (
        "Run `tools/spec-check.sh` for the Exsecutor workspace. Runs outside `nix develop` -- "
        "it needs no toolchain. Output is truncated to the last 200 lines / 32 KiB inline; the "
        "full log is written to the artifact path in the result. A non-zero exit is reported "
        "in the result (isError is set), never raised."
    )

    @tool("spec_check", desc, {"type": "object", "properties": {}})
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        res = await _run(ctx, ["tools/spec-check.sh"], tool="spec_check", nix=False)
        return _format(res)

    return handler


def _gate_tool(ctx: Any) -> Any:
    desc = (
        "Run `trvthnvke verify --fail` for the Exsecutor workspace, under `nix develop -c` "
        "(the README gate; CLAUDE.md, \"The README gate\"). Output is truncated to the last "
        "200 lines / 32 KiB inline; the full log is written to the artifact path in the "
        "result. A non-zero exit is reported in the result (isError is set), never raised."
    )

    @tool("gate", desc, {"type": "object", "properties": {}})
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        res = await _run(ctx, ["trvthnvke", "verify", "--fail"], tool="gate")
        return _format(res)

    return handler


# ---------------------------------------------------------------------------
# conformance
# ---------------------------------------------------------------------------

_TEST_DIRECTIVE_RE = re.compile(r"^//\s*TEST:")


def _find_fixture_directive(workspace: Path, case: str) -> str | None:
    conf_dir = workspace / "tests" / "conformance"
    if not conf_dir.is_dir():
        return None
    for path in sorted(conf_dir.glob(f"*{case}*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if _TEST_DIRECTIVE_RE.match(stripped):
                return f"{path.name}: {stripped}"
    return None


def _conformance_tool(ctx: Any) -> Any:
    desc = (
        "Run the full Exsecutor conformance suite (`tests/run.sh`) and report the lines of "
        "its output that mention `case`, plus the fixture's own `// TEST: entry=N shape=... "
        "expect-code=EXS-Exxxx status=run|deferred` directive line parsed out of "
        "`tests/conformance/*<case>*`. [OPEN]: this always runs the WHOLE suite -- "
        "`tests/run.sh` has no single-case filter, so there is no cheaper single-fixture mode "
        "yet. Output is truncated to the last 200 lines / 32 KiB inline; the full log is at "
        "the artifact path in the result. A non-zero exit is reported in the result (isError "
        "is set), never raised."
    )
    schema = {
        "type": "object",
        "properties": {"case": {"type": "string", "description": "e.g. \"entry03\" or \"3\""}},
        "required": ["case"],
    }

    @tool("conformance", desc, schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        case = str(args.get("case", "")).strip()
        if not case:
            return _error("conformance: case must not be empty")

        res = await _run(ctx, ["tests/run.sh"], tool="conformance")
        try:
            full_text = Path(res["artifact"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            full_text = res["tail"]

        matching = [line for line in full_text.splitlines() if case in line]
        directive = _find_fixture_directive(Path(ctx.state.workspace), case)

        extra_parts = []
        if matching:
            extra_parts.append(f"lines mentioning {case!r}:")
            extra_parts.extend(matching[:200])
        else:
            extra_parts.append(f"no output line mentions {case!r}")
        extra_parts.append(
            directive if directive else f"no fixture directive found for {case!r} under "
            "tests/conformance/"
        )

        return _format(res, extra="\n".join(extra_parts))

    return handler


# ---------------------------------------------------------------------------
# worktree
# ---------------------------------------------------------------------------


def _summarize_worktree_list(text: str) -> str:
    rows: list[str] = []
    block: dict[str, str] = {}

    def flush() -> None:
        if not block:
            return
        path = block.get("worktree", "?")
        sha = block.get("HEAD", "")[:12]
        if "bare" in block:
            ref = "bare"
        elif "detached" in block:
            ref = "detached"
        else:
            ref = block.get("branch", "?")
        rows.append(f"{path}  {ref}  {sha}")

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            flush()
            block = {}
            continue
        if line in ("bare", "detached"):
            block[line] = ""
            continue
        name, _, rest = line.partition(" ")
        block[name] = rest
    flush()
    return "\n".join(rows) if rows else "(no worktrees reported)"


def _worktree_tool(ctx: Any) -> Any:
    desc = (
        "Manage git worktrees for lictor's edit fence. `op=\"new\"` runs `git worktree add "
        "<workspace>/<worktree_root>/lictor-<name> -b lictor/<name> <base>` (base defaults to "
        "HEAD); on success it points lictor's active worktree and cwd at the new path and the "
        "result ends with the exact line RECONNECT_REQUIRED -- this tool does NOT reconnect "
        "the Claude session itself, the caller must watch for that line and reconnect. "
        "`op=\"list\"` runs `git worktree list --porcelain` and summarises one line per "
        "worktree. `op=\"remove\"` runs `git worktree remove <path>` for the same "
        "`lictor-<name>` path; `--force` anywhere in the arguments is refused outright, never "
        "run. `name` must match `^[a-z0-9][a-z0-9-]{0,40}$`. A non-zero exit is reported in "
        "the result (isError is set), never raised; the full log is at the artifact path."
    )
    schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["new", "list", "remove"]},
            "name": {"type": "string"},
            "base": {"type": "string"},
        },
        "required": ["op"],
    }

    @tool("worktree", desc, schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        if any("--force" in str(value) for value in args.values()):
            return _error("worktree: refused -- --force is not permitted through this tool")

        op = str(args.get("op", ""))
        if op not in ("new", "list", "remove"):
            return _error(f"worktree: unknown op {op!r} (expected new|list|remove)")

        workspace = Path(ctx.state.workspace)
        wt_root = workspace / ctx.cfg.exs.worktree_root

        if op == "list":
            res = await _run(ctx, ["git", "worktree", "list", "--porcelain"], tool="worktree", nix=False)
            try:
                full_text = Path(res["artifact"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                full_text = res["tail"]
            return _format(res, extra=_summarize_worktree_list(full_text))

        name = str(args.get("name") or "")
        if not _NAME_RE.match(name):
            return _error(f"worktree: name {name!r} must match ^[a-z0-9][a-z0-9-]{{0,40}}$")
        path = wt_root / f"lictor-{name}"

        if op == "new":
            base = str(args.get("base") or "HEAD")
            branch = f"lictor/{name}"
            wt_root.mkdir(parents=True, exist_ok=True)
            res = await _run(
                ctx,
                ["git", "worktree", "add", str(path), "-b", branch, base],
                tool="worktree",
                nix=False,
            )
            out = _format(res)
            if not _is_failure(res):
                ctx.state.active_worktree = path
                ctx.state.cwd = path
                out["content"][0]["text"] += "\nRECONNECT_REQUIRED"
            return out

        # op == "remove"
        res = await _run(ctx, ["git", "worktree", "remove", str(path)], tool="worktree", nix=False)
        return _format(res)

    return handler


# ---------------------------------------------------------------------------
# spec / codes -- pure python, no subprocess
# ---------------------------------------------------------------------------

_SECTION_QUERY_RE = re.compile(r"^(\d+)(?:\.(\d+))?$")
_TOP_HEADING_RE = re.compile(r"^#\s+\d")
_ROW_RE = re.compile(r"^\|\s*`(EXS-E\d+)`\s*\|\s*(.*?)\s*\|\s*$")

_SPEC_RELATIVE_PATH = Path("docs") / "spec" / "exsecutor-spec-v0.4.md"
_CODES_INC_RELATIVE_PATH = Path("compiler") / "x86_64" / "diag" / "codes.inc"


def _normalize_section(raw: str) -> tuple[str, str | None] | None:
    s = raw.strip()
    if s.startswith("§"):
        s = s[1:].strip()
    m = _SECTION_QUERY_RE.match(s)
    if not m:
        return None
    return m.group(1), m.group(2)


def _slice_spec(text: str, section: str) -> tuple[str, int, int] | None:
    """Return (slice_text, 1-based start line, 1-based end line), or None
    if `section` cannot be parsed or is not present."""
    parsed = _normalize_section(section)
    if parsed is None:
        return None
    top, sub = parsed
    lines = text.splitlines()

    if sub is None:
        start_re = re.compile(rf"^#\s+{re.escape(top)}\.")
        boundary_re = re.compile(r"^#\s")
    else:
        start_re = re.compile(rf"^##\s+{re.escape(top)}\.{re.escape(sub)}\b")
        boundary_re = re.compile(r"^#{1,2}\s")

    start_idx = None
    for i, line in enumerate(lines):
        if start_re.match(line):
            start_idx = i
            break
    if start_idx is None:
        return None

    end_idx = len(lines)
    for j in range(start_idx + 1, len(lines)):
        if boundary_re.match(lines[j]):
            end_idx = j
            break

    return "\n".join(lines[start_idx:end_idx]), start_idx + 1, end_idx


def _spec_tool(ctx: Any) -> Any:
    desc = (
        "Slice a section out of docs/spec/exsecutor-spec-v0.4.md in the Exsecutor workspace, "
        "pure Python -- no subprocess. Accepts \"13\", \"§13\", \"8.3\" or \"§8.3\". A "
        "top-level section N runs from the line matching ^# N\\. to the next ^# line; a "
        "subsection N.M from ^## N\\.M\\b to the next ^#{1,2} line. Returns the slice with a "
        "header naming the file and the 1-based line range, capped at 60 KiB (noted if "
        "truncated). If the section is absent, says so and lists the nearest top-level "
        "headings instead."
    )
    schema = {
        "type": "object",
        "properties": {"section": {"type": "string"}},
        "required": ["section"],
    }

    @tool("spec", desc, schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        section = str(args.get("section", ""))
        spec_path = Path(ctx.state.workspace) / _SPEC_RELATIVE_PATH
        if not spec_path.is_file():
            return _error(f"spec: no spec file at {spec_path}")

        text = spec_path.read_text(encoding="utf-8")
        result = _slice_spec(text, section)
        if result is None:
            headings = [line for line in text.splitlines() if _TOP_HEADING_RE.match(line)]
            msg = (
                f"spec: no section {section!r} found in {spec_path}\n"
                "nearest headings:\n" + "\n".join(headings[:40])
            )
            return _error(msg)

        slice_text, start, end = result
        full = f"{spec_path} lines {start}-{end}\n\n{slice_text}"
        data = full.encode("utf-8")
        if len(data) > _SPEC_MAX_BYTES:
            full = data[:_SPEC_MAX_BYTES].decode("utf-8", errors="ignore") + "\n\n[truncated at 60 KiB]"

        return {"content": [{"type": "text", "text": full}]}

    return handler


def _codes_tool(ctx: Any) -> Any:
    desc = (
        "Search the §13 error registry in docs/spec/exsecutor-spec-v0.4.md (a Markdown table "
        "of rows shaped `| `EXS-E0101` | meaning |`) in the Exsecutor workspace, pure Python "
        "-- no subprocess. Matches `query` case-insensitively against the code and the "
        "meaning. For every hit, also reports whether the code appears in "
        "compiler/x86_64/diag/codes.inc (plain substring search) so drift between the "
        "registry and the generated table is visible. Never invents or renumbers a code -- "
        "it only ever reports what §13 and codes.inc already say."
    )
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }

    @tool("codes", desc, schema)
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return _error("codes: query must not be empty")

        spec_path = Path(ctx.state.workspace) / _SPEC_RELATIVE_PATH
        if not spec_path.is_file():
            return _error(f"codes: no spec file at {spec_path}")
        text = spec_path.read_text(encoding="utf-8")

        sliced = _slice_spec(text, "13")
        if sliced is None:
            return _error(f"codes: could not locate §13 in {spec_path}")
        section_text = sliced[0]

        rows: list[tuple[str, str]] = []
        for line in section_text.splitlines():
            m = _ROW_RE.match(line.strip())
            if m:
                rows.append((m.group(1), m.group(2)))

        q = query.lower()
        hits = [(code, meaning) for code, meaning in rows if q in code.lower() or q in meaning.lower()]
        if not hits:
            return _error(f"codes: no §13 row matches {query!r} ({len(rows)} rows scanned)")

        codes_inc_path = Path(ctx.state.workspace) / _CODES_INC_RELATIVE_PATH
        have_codes_inc = codes_inc_path.is_file()
        codes_inc_text = codes_inc_path.read_text(encoding="utf-8", errors="replace") if have_codes_inc else ""

        lines_out = [f"§13 rows in {spec_path} matching {query!r}:"]
        for code, meaning in hits:
            if not have_codes_inc:
                drift = f"codes.inc not found at {codes_inc_path}"
            elif code in codes_inc_text:
                drift = "present in codes.inc"
            else:
                drift = "** DRIFT: absent from codes.inc **"
            lines_out.append(f"{code}  {meaning}  [{drift}]")

        return {"content": [{"type": "text", "text": "\n".join(lines_out)}]}

    return handler


# ---------------------------------------------------------------------------
# registry entry point
# ---------------------------------------------------------------------------


def build(ctx: Any) -> list[Any]:
    return [
        _build_tool(ctx),
        _test_tool(ctx),
        _spec_check_tool(ctx),
        _audit_tool(ctx),
        _reproduce_tool(ctx),
        _check_tool(ctx),
        _smoke_tool(ctx),
        _conformance_tool(ctx),
        _gate_tool(ctx),
        _worktree_tool(ctx),
        _spec_tool(ctx),
        _codes_tool(ctx),
    ]
