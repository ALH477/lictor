"""XDG paths and config.toml loading for lictor.

Precedence, lowest to highest: packaged dataclass defaults < the user config
at ``<config>/config.toml`` < the workspace config at
``<workspace>/.lictor/config.toml`` < CLI flags (applied by the caller via
:meth:`Config.apply_cli_overrides`).
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: Subdirectories ensure() creates under the state directory.
_STATE_SUBDIRS = (
    "conversations",
    "artifacts",
    "vault",
    "overlay",
    "generations",
    "crash",
    "inferences",
)


def _xdg_dir(specific_env: str, base_env: str, fallback: str) -> Path:
    """Resolve one lictor XDG directory.

    ``$<specific_env>`` wins outright (used verbatim, no ``lictor`` suffix
    appended -- the caller asked for an exact location). Otherwise
    ``$<base_env>/lictor`` if the base is set, else ``~/<fallback>/lictor``.
    """
    specific = os.environ.get(specific_env)
    if specific:
        return Path(specific)
    base = os.environ.get(base_env)
    base_path = Path(base) if base else (Path.home() / fallback)
    return base_path / "lictor"


@dataclass(frozen=True)
class Paths:
    """Resolved XDG-ish locations for lictor's own state/data/config."""

    state: Path
    data: Path
    config: Path

    @classmethod
    def resolve(cls) -> Paths:
        return cls(
            state=_xdg_dir("LICTOR_STATE_HOME", "XDG_STATE_HOME", ".local/state"),
            data=_xdg_dir("LICTOR_DATA_HOME", "XDG_DATA_HOME", ".local/share"),
            config=_xdg_dir("LICTOR_CONFIG_HOME", "XDG_CONFIG_HOME", ".config"),
        )

    def ensure(self) -> Paths:
        """Create every directory this version of lictor needs. Returns self."""
        for name in _STATE_SUBDIRS:
            (self.state / name).mkdir(parents=True, exist_ok=True)
        self.data.mkdir(parents=True, exist_ok=True)
        self.config.mkdir(parents=True, exist_ok=True)
        return self


@dataclass
class WorkspaceConfig:
    path: str = field(default_factory=os.getcwd)


@dataclass
class ClaudeConfig:
    cli_path: str | None = None
    model: str = ""
    effort: str = "medium"
    permission_mode: str = "default"
    setting_sources: list[str] = field(default_factory=lambda: ["user", "project", "local"])


@dataclass
class RlmConfig:
    turn_budget: int = 40
    concurrency: int = 3
    model: str = "haiku"


@dataclass
class ExsConfig:
    timeout_s: int = 900
    worktree_root: str = ".claude/worktrees"


@dataclass
class SandboxConfig:
    bwrap: str = "auto"  # auto | off | require


@dataclass
class Config:
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    rlm: RlmConfig = field(default_factory=RlmConfig)
    exs: ExsConfig = field(default_factory=ExsConfig)
    sandbox: SandboxConfig = field(default_factory=SandboxConfig)

    @classmethod
    def load(cls, paths: Paths, *, workspace_override: str | None = None) -> Config:
        """Load packaged defaults, then the user config, then the workspace
        config (the workspace being ``workspace_override`` if given, else
        whatever the user config says, else ``os.getcwd()``)."""
        cfg = cls()
        _apply_toml(cfg, paths.config / "config.toml")

        workspace_dir = Path(workspace_override) if workspace_override else Path(cfg.workspace.path)
        _apply_toml(cfg, workspace_dir / ".lictor" / "config.toml")

        if workspace_override:
            cfg.workspace.path = str(workspace_dir)
        return cfg

    def apply_cli_overrides(
        self,
        *,
        workspace: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        mode: str | None = None,
    ) -> None:
        """CLI flags win over every config file. Call after :meth:`load`."""
        if workspace is not None:
            self.workspace.path = workspace
        if model is not None:
            self.claude.model = model
        if effort is not None:
            self.claude.effort = effort
        if mode is not None:
            self.claude.permission_mode = mode

    def resolve_cli_path(self) -> str | None:
        """``$LICTOR_CLAUDE`` wins, then the config value, then ``PATH``."""
        return (
            os.environ.get("LICTOR_CLAUDE")
            or self.claude.cli_path
            or shutil.which("claude")
        )


def _apply_toml(cfg: Config, path: Path) -> None:
    if not path.is_file():
        return
    data: dict[str, Any] = tomllib.loads(path.read_text(encoding="utf-8"))
    for section_name, section_obj in (
        ("workspace", cfg.workspace),
        ("claude", cfg.claude),
        ("rlm", cfg.rlm),
        ("exs", cfg.exs),
        ("sandbox", cfg.sandbox),
    ):
        section_data = data.get(section_name)
        if not isinstance(section_data, dict):
            continue
        for key, value in section_data.items():
            if hasattr(section_obj, key):
                setattr(section_obj, key, value)
