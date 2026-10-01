"""lictor launcher (wave 0: --version only; the REPL lands in wave 1)."""

from __future__ import annotations

import importlib.metadata
import os
import shutil
import subprocess
import sys

from . import __version__


def _claude_version(cli_path: str | None) -> str:
    if cli_path is None:
        return "claude: not found"
    try:
        out = subprocess.run(
            [cli_path, "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        text = (out.stdout or out.stderr).strip()
        return f"claude: {text}" if text else "claude: not found"
    except OSError:
        return "claude: not found"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    if "--version" in argv:
        print(f"lictor: {__version__}")
        try:
            sdk_version = importlib.metadata.version("claude-agent-sdk")
        except importlib.metadata.PackageNotFoundError:
            sdk_version = "not found"
        print(f"claude-agent-sdk: {sdk_version}")
        cli_path = os.environ.get("LICTOR_CLAUDE") or shutil.which("claude")
        print(_claude_version(cli_path))
        return 0

    print("lictor: REPL not implemented yet (wave 1)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
