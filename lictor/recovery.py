"""Crash capture, sanitization, and the boot-marker protocol that decides
whether a launch is a normal boot or a recovery boot.

The launcher (`__main__.py`, not owned by this milestone) is expected to
call `write_starting()` before it replays the overlay journal, `mark_booted()`
once the first prompt is up, and `clear()` on a clean exit; `decide_boot()`
tells it, on the way in, whether the *previous* process's `boot.json` was
still in `phase="starting"` when this one started -- the signature of a
crash during startup, which is answered with a recovery boot (no overlay
replay, no `init.py`) rather than repeating whatever just failed.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: nix store paths look like /nix/store/<32-char base32 hash>-<name>.
_STORE_RE = re.compile(r"/nix/store/[0-9a-z]{32}-")

#: token-shaped runs: explicit provider prefixes, and generic long
#: base64url-ish/base64-ish spans (secrets, but also incidentally long
#: hashes -- sanitize favors over-redaction).
_TOKEN_RES = (
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"gho_[A-Za-z0-9]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),
    re.compile(r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/_-]{24,}={0,2}(?![A-Za-z0-9+/=_-])"),
)


def _boot_path(paths: Any) -> Path:
    return Path(paths.state) / "boot.json"


def _crash_dir(paths: Any) -> Path:
    return Path(paths.state) / "crash"


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# -- boot marker ------------------------------------------------------------


def write_starting(paths: Any, gen: int) -> None:
    _atomic_write_json(_boot_path(paths), {"pid": os.getpid(), "t": _now_iso(), "gen": gen, "phase": "starting"})


def mark_booted(paths: Any) -> None:
    path = _boot_path(paths)
    data: dict[str, Any] = {"pid": os.getpid(), "t": _now_iso(), "gen": 0, "phase": "booted"}
    if path.exists():
        try:
            data.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    data["phase"] = "booted"
    _atomic_write_json(path, data)


def clear(paths: Any) -> None:
    try:
        _boot_path(paths).unlink()
    except FileNotFoundError:
        pass


# -- sanitization -------------------------------------------------------


def sanitize(text: str) -> str:
    """`$HOME` -> `~`, nix store hashes collapsed, token-shaped runs
    redacted. Never includes an env value -- callers must not hand this
    one either; this only scrubs *paths and secret-shaped substrings
    already present in `text`."""
    home = str(Path.home())
    if home and home != "/":
        text = text.replace(home, "~")
    text = _STORE_RE.sub("<store>/", text)
    for rx in _TOKEN_RES:
        text = rx.sub("<redacted>", text)
    return text


def _tail_log(paths: Any, n: int) -> list[str]:
    log_path = Path(paths.state) / "lictor.log"
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return lines[-n:]


def _versions() -> dict[str, str]:
    import importlib.metadata

    from . import __version__ as lictor_version

    try:
        sdk_version = importlib.metadata.version("claude-agent-sdk")
    except importlib.metadata.PackageNotFoundError:
        sdk_version = "not found"
    return {"lictor": lictor_version, "sdk": sdk_version}


def capture_crash(paths: Any, exc: BaseException) -> Path:
    """Write `<state>/crash/<ts>.json`: the sanitized traceback, lictor/sdk
    versions, the active generation, and the last 60 lines of the log --
    never an env value. Returns the path written."""
    from . import overlay as overlay_mod

    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    versions = _versions()
    try:
        generation = overlay_mod.active_generation(paths)
    except Exception:  # noqa: BLE001 -- a broken overlay dir must not block crash capture
        generation = None

    data = {
        "t": _now_iso(),
        "traceback": sanitize(tb_text),
        "lictor": versions["lictor"],
        "sdk": versions["sdk"],
        "generation": generation,
        "log_tail": [sanitize(line) for line in _tail_log(paths, 60)],
    }

    ts = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = _crash_dir(paths) / f"{ts}.json"
    _atomic_write_json(path, data)
    return path


def _latest_crash(paths: Any) -> Path | None:
    d = _crash_dir(paths)
    if not d.is_dir():
        return None
    files = sorted(d.glob("*.json"))
    return files[-1] if files else None


def _crash_summary(path: Path) -> str | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    tb = data.get("traceback") or ""
    lines = [line for line in tb.splitlines() if line.strip()]
    return lines[-1] if lines else None


# -- the boot decision --------------------------------------------------


@dataclass
class BootDecision:
    recovery: bool
    reason: str
    crash_path: Path | None = None
    crash_summary: str | None = None


def decide_boot(paths: Any, forced_recovery: bool) -> BootDecision:
    """Recovery is forced by `--recovery`, or implied by a `boot.json`
    still in `phase="starting"` -- the previous process died during
    startup, before it ever reached a prompt. Either way, the newest crash
    file's sanitized summary rides along for the banner; a crash *after*
    boot never forces recovery on its own, but still gets surfaced."""
    boot_path = _boot_path(paths)
    stale_starting = False
    if boot_path.exists():
        try:
            data = json.loads(boot_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        stale_starting = data.get("phase") == "starting"

    crash_path = _latest_crash(paths)
    crash_summary = _crash_summary(crash_path) if crash_path is not None else None

    if forced_recovery or stale_starting:
        reason = "forced by --recovery" if forced_recovery and not stale_starting else (
            "the previous boot died during startup"
        )
        return BootDecision(recovery=True, reason=reason, crash_path=crash_path, crash_summary=crash_summary)

    return BootDecision(recovery=False, reason="clean", crash_path=crash_path, crash_summary=crash_summary)


# -- sys.excepthook ----------------------------------------------------------


def install(paths: Any) -> None:
    """Install a `sys.excepthook` that captures a sanitized crash report
    before falling through to the interpreter's default handler."""
    original_hook = sys.excepthook

    def _hook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
        try:
            capture_crash(paths, exc)
        except Exception:  # noqa: BLE001, S110 -- crash capture must never mask the real crash
            pass
        original_hook(exc_type, exc, tb)

    sys.excepthook = _hook
