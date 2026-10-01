"""The overlay journal: proposed/exercised/committed/discarded mutations,
generations, and the private git history that backs them.

A mutation is never applied to the real `lictor/` source tree. It is
compiled as its own module (``lictor._overlay.<seq>``), applied live to a
``ToolRegistry`` or to a ``lictor.*`` module attribute, and -- only once an
operator has exercised it and asked to commit -- replay-probed in a clean
subprocess and folded into a private git history at
``<state>/mutations.git``, whose work tree is ``<state>/overlay``. Overlay
``.py`` files are never deleted: history here is append-only, and
``/rollback`` only ever repoints ``<state>/active-generation``.

Two targets exist, chosen by a scheme prefix on the ``target`` string:

* ``tool:<ns>.<name>`` -- rebinds a registered ``SdkMcpTool``'s handler.
  Its wire schema can never change this way (the SDK fixes the tool list
  and schemas when the server is built), so a schema mismatch refuses.
* ``fn:<module>:<attr>`` -- ``setattr``s a ``lictor.*`` module attribute
  that already exists.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.metadata
import inspect
import json
import os
import subprocess
import sys
import types
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import __version__ as LICTOR_VERSION

#: process-local record of what a seq's `apply()` replaced, keyed by
#: (str(state dir), seq) so `discard()` can restore it -- but only within
#: the same process that applied it. A fresh process that replays history
#: has nothing to restore *to* for a discarded entry: it simply never
#: applies it.
_APPLIED: dict[tuple[str, int], ApplyOutcome] = {}

#: cached (lictor, sdk, claude) version strings, keyed by resolved cli_path
#: so tests that point at different fake binaries don't see a stale value.
_VERSION_CACHE: dict[str | None, dict[str, str]] = {}


class RedefineRefused(Exception):
    """A redefine/commit/discard/rollback step refuses to proceed. The
    message is the explanation shown to the operator or the model; never a
    bare assertion."""


# -- small dataclasses ---------------------------------------------------


@dataclass
class ShadowInfo:
    path: str
    file_sha256: str
    symbol: str
    symbol_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ShadowInfo:
        return cls(path=d["path"], file_sha256=d["file_sha256"], symbol=d["symbol"],
                    symbol_sha256=d["symbol_sha256"])


@dataclass
class OverlayEntry:
    seq: int
    target: str
    created: str
    cid: str | None
    lictor: str
    sdk: str
    claude: str
    shadows: ShadowInfo
    status: str = "proposed"  # proposed | exercised | committed | discarded
    exercise: dict[str, Any] | None = None
    commit: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["shadows"] = self.shadows.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> OverlayEntry:
        return cls(
            seq=d["seq"],
            target=d["target"],
            created=d["created"],
            cid=d.get("cid"),
            lictor=d.get("lictor", ""),
            sdk=d.get("sdk", ""),
            claude=d.get("claude", ""),
            shadows=ShadowInfo.from_dict(d["shadows"]),
            status=d.get("status", "proposed"),
            exercise=d.get("exercise"),
            commit=d.get("commit"),
        )

    def slug(self) -> str:
        return _slugify(self.target)

    def source_path(self, paths: Any) -> Path:
        return overlay_dir(paths) / f"{self.seq:04d}-{self.slug()}.py"

    def manifest_path(self, paths: Any) -> Path:
        return overlay_dir(paths) / f"{self.seq:04d}-{self.slug()}.json"

    def save(self, paths: Any) -> None:
        _atomic_write_json(self.manifest_path(paths), self.to_dict())


@dataclass
class ApplyOutcome:
    kind: str  # "tool" | "fn"
    key: str  # dotted tool name, or "<module>:<attr>" (informational)
    previous: Any
    registry: Any = None
    module: Any = None  # the live module object, for kind == "fn"
    attr: str | None = None  # the attribute name, for kind == "fn"


@dataclass
class ReplaySkip:
    seq: int
    target: str
    message: str


@dataclass
class ReplayReport:
    applied: list[int] = field(default_factory=list)
    skipped: list[ReplaySkip] = field(default_factory=list)


@dataclass
class CommitResult:
    ok: bool
    probe: dict[str, Any]
    entry: OverlayEntry
    generation: int | None = None
    commit: str | None = None


# -- paths ----------------------------------------------------------------


def overlay_dir(paths: Any) -> Path:
    return Path(paths.state) / "overlay"


def generations_dir(paths: Any) -> Path:
    return Path(paths.state) / "generations"


def active_generation_path(paths: Any) -> Path:
    return Path(paths.state) / "active-generation"


def mutations_git_dir(paths: Any) -> Path:
    return Path(paths.state) / "mutations.git"


def _slugify(target: str) -> str:
    slug = target.replace(":", "-").replace(".", "-")
    slug = "".join(c if (c.isalnum() or c == "-" or c == "_") else "-" for c in slug)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-").lower() or "target"


def entry_paths(paths: Any, seq: int, target: str) -> tuple[Path, Path]:
    slug = _slugify(target)
    d = overlay_dir(paths)
    return d / f"{seq:04d}-{slug}.py", d / f"{seq:04d}-{slug}.json"


def _now_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# -- the journal: listing and loading --------------------------------------


def list_entries(paths: Any) -> list[OverlayEntry]:
    d = overlay_dir(paths)
    if not d.is_dir():
        return []
    entries = []
    for p in sorted(d.glob("*.json")):
        try:
            entries.append(OverlayEntry.from_dict(json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, json.JSONDecodeError, KeyError):
            continue
    entries.sort(key=lambda e: e.seq)
    return entries


def load_entry(paths: Any, seq: int) -> OverlayEntry:
    for entry in list_entries(paths):
        if entry.seq == seq:
            return entry
    raise RedefineRefused(f"no overlay entry with seq {seq}")


def latest_entry_for_target(paths: Any, target: str) -> OverlayEntry | None:
    """The highest-seq entry proposed for `target`, of any status."""
    matches = [e for e in list_entries(paths) if e.target == target]
    return matches[-1] if matches else None


def _resolve_entry(paths: Any, entry_or_seq: OverlayEntry | int) -> OverlayEntry:
    if isinstance(entry_or_seq, OverlayEntry):
        # Re-read from disk: the caller's copy may predate a later save().
        return load_entry(paths, entry_or_seq.seq)
    return load_entry(paths, int(entry_or_seq))


def next_seq(paths: Any) -> int:
    d = overlay_dir(paths)
    if not d.is_dir():
        return 1
    best = 0
    for p in d.glob("*"):
        stem = p.name.split("-", 1)[0]
        if stem.isdigit():
            best = max(best, int(stem))
    return best + 1


# -- versions ---------------------------------------------------------------


def versions(cfg: Any) -> dict[str, str]:
    cli_path = cfg.resolve_cli_path() if cfg is not None else None
    cached = _VERSION_CACHE.get(cli_path)
    if cached is not None:
        return cached

    try:
        sdk_version = importlib.metadata.version("claude-agent-sdk")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = "not found"

    claude_version = "not found"
    if cli_path:
        try:
            out = subprocess.run(
                [cli_path, "--version"], capture_output=True, text=True, check=False, timeout=5
            )
            claude_version = (out.stdout or out.stderr).strip() or "not found"
        except (OSError, subprocess.TimeoutExpired):
            claude_version = "not found"

    result = {"lictor": LICTOR_VERSION, "sdk": sdk_version, "claude": claude_version}
    _VERSION_CACHE[cli_path] = result
    return result


# -- locating the tracked definition (shadows) -------------------------------


def _split_target(target: str) -> tuple[str, str]:
    if ":" not in target:
        raise RedefineRefused(f"malformed redefine target (missing scheme): {target!r}")
    kind, rest = target.split(":", 1)
    if kind not in ("tool", "fn"):
        raise RedefineRefused(f"unknown redefine target scheme {kind!r} in {target!r}")
    return kind, rest


def split_target(target: str) -> tuple[str, str]:
    """Public form of `_split_target`, for callers outside this module
    (``tools/self_.py``) that need to tell a ``tool:`` target from an
    ``fn:`` one without re-deriving the scheme grammar."""
    return _split_target(target)


def _decorator_is_tool(func: ast.AST) -> bool:
    if isinstance(func, ast.Name):
        return func.id == "tool"
    if isinstance(func, ast.Attribute):
        return func.attr == "tool"
    return False


def _locate_tool_def(ns: str, name: str) -> tuple[Path, ast.AST, str]:
    from . import tools as tools_pkg

    tools_dir = Path(tools_pkg.__file__).parent
    reverse_ns = {v: k for k, v in tools_pkg.MODULE_NAMESPACE.items()}
    preferred = reverse_ns.get(ns, ns)

    candidates = [tools_dir / f"{preferred}.py"]
    for p in sorted(tools_dir.glob("*.py")):
        if p.name != "__init__.py" and p not in candidates:
            candidates.append(p)

    for path in candidates:
        if not path.is_file():
            continue
        source_text = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source_text, filename=str(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not (isinstance(dec, ast.Call) and _decorator_is_tool(dec.func) and dec.args):
                    continue
                first = dec.args[0]
                if isinstance(first, ast.Constant) and first.value == name:
                    return path, node, source_text
    raise RedefineRefused(f"cannot locate tracked definition for tool:{ns}.{name}")


def _locate_fn_def(mod_name: str, attr: str) -> tuple[Path, ast.AST, str]:
    if not (mod_name == "lictor" or mod_name.startswith("lictor.")):
        raise RedefineRefused(f"fn: redefine refuses non-lictor module {mod_name!r}")
    try:
        module = importlib.import_module(mod_name)
    except ImportError as exc:
        raise RedefineRefused(f"fn: redefine cannot import module {mod_name!r}: {exc}") from exc
    if not hasattr(module, attr):
        raise RedefineRefused(f"fn: redefine requires an existing attribute {mod_name}.{attr}")

    source_file = inspect.getsourcefile(module)
    if source_file is None:
        raise RedefineRefused(f"fn: redefine cannot find a source file for module {mod_name!r}")
    path = Path(source_file)
    source_text = path.read_text(encoding="utf-8")
    tree = ast.parse(source_text, filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == attr:
            return path, node, source_text
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == attr:
                    return path, node, source_text
    raise RedefineRefused(f"cannot locate tracked definition for fn:{mod_name}:{attr}")


def find_definition(target: str) -> tuple[Path, ast.AST, str]:
    """(path, ast node, full file source text) for the tracked definition a
    target names. Public so `self_.diff` / `self_.persist_definition` can
    reach the exact source segment without re-deriving the location logic."""
    kind, rest = _split_target(target)
    if kind == "tool":
        ns, name = rest.split(".", 1)
        return _locate_tool_def(ns, name)
    mod_name, attr = rest.split(":", 1)
    return _locate_fn_def(mod_name, attr)


def compute_shadow(target: str) -> ShadowInfo:
    path, node, source_text = find_definition(target)
    segment = ast.get_source_segment(source_text, node)
    if segment is None:
        raise RedefineRefused(f"could not extract source for the tracked definition of {target!r}")
    symbol = node.name if hasattr(node, "name") else target
    return ShadowInfo(
        path=str(path),
        file_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        symbol=symbol,
        symbol_sha256=hashlib.sha256(segment.encode("utf-8")).hexdigest(),
    )


# -- schema comparison --------------------------------------------------


def _schema_signature(schema: Any) -> Any:
    """A best-effort, order-independent, structural fingerprint of a
    ``@tool`` input schema. Plain-dict and JSON-schema-dict schemas compare
    exactly; two separately-defined ``TypedDict`` classes compare by their
    resolved field names/types rather than by class identity, which would
    otherwise always differ between the tracked module and a freshly
    compiled overlay module even when nothing meaningfully changed."""
    if isinstance(schema, dict):
        return {k: _schema_signature(v) for k, v in sorted(schema.items(), key=repr)}
    if isinstance(schema, (list, tuple)):
        return [_schema_signature(v) for v in schema]
    if isinstance(schema, type):
        try:
            import typing

            hints = typing.get_type_hints(schema, include_extras=True)
        except Exception:  # noqa: BLE001 -- unresolvable hints just fall back to repr below
            hints = {}
        if hints:
            return {k: repr(v) for k, v in sorted(hints.items())}
    return repr(schema)


def schemas_match(a: Any, b: Any) -> bool:
    return _schema_signature(a) == _schema_signature(b)


# -- compiling and applying an overlay module ----------------------------


def exec_overlay_module(seq: int, path: Path, source: str) -> types.ModuleType:
    """Compile+exec `source` as `lictor._overlay.<seq>`, with `__file__` set
    to `path` so a traceback raised from it names the overlay file. Left
    registered in `sys.modules` on success; removed on failure."""
    module_name = f"lictor._overlay.{seq}"
    module = types.ModuleType(module_name)
    module.__file__ = str(path)
    sys.modules[module_name] = module
    try:
        code = compile(source, str(path), "exec")
        exec(code, module.__dict__)  # noqa: S102 -- this *is* the milestone
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


def _is_sdk_mcp_tool(value: Any) -> bool:
    try:
        from claude_agent_sdk import SdkMcpTool
    except ImportError:
        return False
    return isinstance(value, SdkMcpTool)


def apply(paths: Any, registry: Any, seq: int, path: Path, source: str, target: str) -> ApplyOutcome:
    """Compile `source` and apply it live: rebind a tool's handler, or
    setattr a lictor.* module attribute. Raises `RedefineRefused` on a
    schema mismatch or a malformed overlay; propagates any exception the
    overlay source itself raises while executing (its traceback names the
    overlay file -- see `exec_overlay_module`)."""
    module = exec_overlay_module(seq, path, source)
    kind, rest = _split_target(target)

    if kind == "tool":
        dotted = rest
        if dotted not in registry.tools:
            raise RedefineRefused(f"tool:{dotted} is not a registered tool")

        candidates_tool = [v for v in vars(module).values() if _is_sdk_mcp_tool(v)]
        candidates_fn = [v for v in vars(module).values() if inspect.iscoroutinefunction(v)]

        if candidates_tool and not candidates_fn and len(candidates_tool) == 1:
            new_tool = candidates_tool[0]
            current_schema = registry.tools[dotted].input_schema
            if not schemas_match(current_schema, new_tool.input_schema):
                raise RedefineRefused(
                    f"tool:{dotted}: a schema change needs a restart -- the wire tool list and "
                    "schemas are fixed when the MCP server is created"
                )
            handler = new_tool.handler
        elif candidates_fn and not candidates_tool and len(candidates_fn) == 1:
            handler = candidates_fn[0]
        elif not candidates_tool and not candidates_fn:
            raise RedefineRefused(
                f"tool:{dotted}: overlay source defines no @tool object and no async function"
            )
        else:
            raise RedefineRefused(
                f"tool:{dotted}: overlay source must define exactly one @tool object or one async "
                "function"
            )

        previous = registry.rebind(dotted, handler)
        outcome = ApplyOutcome(kind="tool", key=dotted, previous=previous, registry=registry)

    else:  # kind == "fn"
        mod_name, attr = rest.split(":", 1)
        if not (mod_name == "lictor" or mod_name.startswith("lictor.")):
            raise RedefineRefused(f"fn: redefine refuses non-lictor module {mod_name!r}")
        target_module = importlib.import_module(mod_name)
        if not hasattr(target_module, attr):
            raise RedefineRefused(f"fn: redefine requires an existing attribute {mod_name}.{attr}")
        if not hasattr(module, attr):
            raise RedefineRefused(f"fn:{mod_name}:{attr}: overlay source does not define {attr!r}")

        new_value = getattr(module, attr)
        previous = getattr(target_module, attr)
        setattr(target_module, attr, new_value)
        outcome = ApplyOutcome(
            kind="fn", key=f"{mod_name}:{attr}", previous=previous, module=target_module, attr=attr
        )

    _APPLIED[(str(Path(paths.state)), seq)] = outcome
    return outcome


# -- the high-level journal operations ------------------------------------


def redefine(
    paths: Any,
    cfg: Any,
    cid: str | None,
    registry: Any,
    target: str,
    source: str,
    records: Any = None,
) -> OverlayEntry:
    """Propose + apply a mutation in one step. On success, writes the
    overlay source file and a `status="proposed"` manifest and returns the
    entry. On failure (schema refusal, bad target, or the overlay source
    itself raising), the source file is left on disk for forensics but no
    manifest is written, and the exception propagates."""
    shadow = compute_shadow(target)
    seq = next_seq(paths)
    py_path, _json_path = entry_paths(paths, seq, target)
    py_path.parent.mkdir(parents=True, exist_ok=True)
    py_path.write_text(source, encoding="utf-8")

    apply(paths, registry, seq, py_path, source, target)

    v = versions(cfg)
    entry = OverlayEntry(
        seq=seq,
        target=target,
        created=_now_iso(),
        cid=cid,
        lictor=v["lictor"],
        sdk=v["sdk"],
        claude=v["claude"],
        shadows=shadow,
        status="proposed",
    )
    entry.save(paths)
    if records is not None:
        records.append("mutation", {"action": "redefine", "seq": seq, "target": target, "status": "proposed"})
    return entry


def record_exercise(paths: Any, entry_or_seq: OverlayEntry | int, result: dict[str, Any], records: Any = None) -> OverlayEntry:
    entry = _resolve_entry(paths, entry_or_seq)
    if entry.status not in ("proposed", "exercised"):
        raise RedefineRefused(f"cannot exercise seq {entry.seq}: status is {entry.status!r}")
    entry.exercise = result
    entry.status = "exercised"
    entry.save(paths)
    if records is not None:
        records.append("mutation", {"action": "exercise", "seq": entry.seq, "ok": result.get("ok")})
    return entry


def discard(paths: Any, entry_or_seq: OverlayEntry | int, records: Any = None) -> OverlayEntry:
    entry = _resolve_entry(paths, entry_or_seq)
    if entry.status == "committed":
        raise RedefineRefused(f"discard refuses seq {entry.seq}: already committed, history is immutable")

    key = (str(Path(paths.state)), entry.seq)
    outcome = _APPLIED.pop(key, None)
    if outcome is not None:
        if outcome.kind == "tool":
            outcome.registry.rebind(outcome.key, outcome.previous)
        else:
            setattr(outcome.module, outcome.attr, outcome.previous)

    entry.status = "discarded"
    entry.save(paths)
    if records is not None:
        records.append("mutation", {"action": "discard", "seq": entry.seq})
    return entry


# -- replay -----------------------------------------------------------------


def replay(paths: Any, seqs: list[int], registry: Any, records: Any = None) -> ReplayReport:
    """Re-apply committed overlays, in the given order, to a freshly built
    registry (startup, or the probe). Never raises: an entry whose tracked
    definition has moved since publication -- or that otherwise fails to
    apply -- is skipped and reported, not fatal to the rest of the replay."""
    report = ReplayReport()
    for seq in seqs:
        try:
            entry = load_entry(paths, seq)
        except RedefineRefused as exc:
            report.skipped.append(ReplaySkip(seq=seq, target="<unknown>", message=str(exc)))
            continue

        try:
            current_shadow = compute_shadow(entry.target)
        except RedefineRefused as exc:
            report.skipped.append(ReplaySkip(seq=seq, target=entry.target, message=str(exc)))
            continue

        if current_shadow.symbol_sha256 != entry.shadows.symbol_sha256:
            report.skipped.append(
                ReplaySkip(seq=seq, target=entry.target, message="tracked definition moved since publication")
            )
            continue

        try:
            source = entry.source_path(paths).read_text(encoding="utf-8")
            apply(paths, registry, entry.seq, entry.source_path(paths), source, entry.target)
        except Exception as exc:  # noqa: BLE001 -- replay reports, it never raises
            report.skipped.append(ReplaySkip(seq=seq, target=entry.target, message=f"{type(exc).__name__}: {exc}"))
            continue

        report.applied.append(seq)

    if records is not None:
        records.append(
            "mutation",
            {"action": "replay", "applied": report.applied, "skipped": [vars(s) for s in report.skipped]},
        )
    return report


# -- the replay probe, run as a clean subprocess -----------------------------


def run_probe(paths: Any, seqs: list[int], timeout: int = 60) -> dict[str, Any]:
    from . import options as options_mod

    env = dict(os.environ)
    options_mod.scrub_environ(env)

    cmd = [
        sys.executable,
        "-m",
        "lictor.probe",
        "--overlay",
        str(overlay_dir(paths)),
        "--seqs",
        ",".join(str(s) for s in seqs),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "checks": [{"name": "probe_subprocess", "ok": False, "detail": f"timed out after {timeout}s"}],
            "ms": timeout * 1000,
        }

    out = proc.stdout.strip()
    if not out:
        return {
            "ok": False,
            "checks": [
                {
                    "name": "probe_subprocess",
                    "ok": False,
                    "detail": f"no stdout; exit={proc.returncode}; stderr={proc.stderr[-2000:]}",
                }
            ],
            "ms": 0,
        }
    try:
        return json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {
            "ok": False,
            "checks": [
                {
                    "name": "probe_subprocess",
                    "ok": False,
                    "detail": f"unparseable stdout; exit={proc.returncode}; stdout={out[-2000:]}",
                }
            ],
            "ms": 0,
        }


# -- commit, generations, rollback -------------------------------------------


def _git_config_get(key: str) -> str | None:
    try:
        out = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, check=False)
    except OSError:
        return None
    value = out.stdout.strip()
    return value or None


def _git(paths: Any, args: list[str]) -> subprocess.CompletedProcess[str]:
    gd = mutations_git_dir(paths)
    wt = overlay_dir(paths)
    return subprocess.run(
        ["git", f"--git-dir={gd}", f"--work-tree={wt}", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _git_init_if_needed(paths: Any) -> None:
    gd = mutations_git_dir(paths)
    if (gd / "HEAD").exists():
        return
    overlay_dir(paths).mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "--bare", "--initial-branch=main", str(gd)], check=True
    )
    name = _git_config_get("user.name") or os.environ.get("USER") or "lictor"
    email = _git_config_get("user.email") or f"{name}@localhost"
    subprocess.run(["git", f"--git-dir={gd}", "config", "user.name", name], check=True)
    subprocess.run(["git", f"--git-dir={gd}", "config", "user.email", email], check=True)


def generations(paths: Any) -> list[dict[str, Any]]:
    d = generations_dir(paths)
    if not d.is_dir():
        return []
    rows = []
    for p in sorted(d.glob("*.json"), key=lambda p: int(p.stem) if p.stem.isdigit() else 0):
        try:
            rows.append(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    rows.sort(key=lambda g: g.get("gen", 0))
    return rows


def _next_generation_number(paths: Any) -> int:
    gens = generations(paths)
    return max((g.get("gen", 0) for g in gens), default=0) + 1


def active_generation(paths: Any) -> int:
    p = active_generation_path(paths)
    if not p.exists():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip())
    except ValueError:
        return 0


def _set_active_generation(paths: Any, n: int) -> None:
    p = active_generation_path(paths)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(str(n), encoding="utf-8")
    os.replace(tmp, p)


def commit(
    paths: Any,
    cfg: Any,
    entry_or_seq: OverlayEntry | int,
    message: str,
    probe_timeout: int = 60,
    records: Any = None,
) -> CommitResult:
    entry = _resolve_entry(paths, entry_or_seq)
    if entry.status != "exercised":
        raise RedefineRefused(f"commit refuses seq {entry.seq}: status is {entry.status!r}, not 'exercised'")

    already_committed = sorted(e.seq for e in list_entries(paths) if e.status == "committed")
    seqs_for_probe = sorted({*already_committed, entry.seq})

    report = run_probe(paths, seqs_for_probe, timeout=probe_timeout)
    if not report.get("ok", False):
        return CommitResult(ok=False, probe=report, entry=entry)

    _git_init_if_needed(paths)
    _git(paths, ["add", "-A"])
    _git(paths, ["commit", "-q", "-m", message])
    sha = _git(paths, ["rev-parse", "HEAD"]).stdout.strip()

    gen_n = _next_generation_number(paths)
    v = versions(cfg)
    generation = {
        "gen": gen_n,
        "created": _now_iso(),
        "mutations_commit": sha,
        "overlay_seqs": seqs_for_probe,
        "lictor": v["lictor"],
        "sdk": v["sdk"],
        "claude": v["claude"],
        "probe": report,
        "note": message,
    }
    _atomic_write_json(generations_dir(paths) / f"{gen_n}.json", generation)
    _set_active_generation(paths, gen_n)

    entry.status = "committed"
    entry.commit = sha
    entry.save(paths)

    if records is not None:
        records.append("mutation", {"action": "commit", "seq": entry.seq, "gen": gen_n, "commit": sha})

    return CommitResult(ok=True, probe=report, entry=entry, generation=gen_n, commit=sha)


def rollback(paths: Any, n: int, records: Any = None) -> int:
    gens = {g["gen"]: g for g in generations(paths)}
    if n != 0:
        if n not in gens:
            raise RedefineRefused(f"rollback refuses: generation {n} is unknown")
        if not gens[n].get("probe", {}).get("ok", False):
            raise RedefineRefused(f"rollback refuses: generation {n} was never probed successfully")

    _set_active_generation(paths, n)
    if records is not None:
        records.append("mutation", {"action": "rollback", "gen": n})
    return n
