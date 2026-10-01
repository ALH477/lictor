"""config.py: defaults, TOML precedence (user < workspace), CLI overrides."""

from __future__ import annotations

import os

from lictor.config import Config


def test_defaults_with_no_toml_files(paths) -> None:
    cfg = Config.load(paths)
    assert cfg.claude.model == ""
    assert cfg.claude.effort == "medium"
    assert cfg.claude.permission_mode == "default"
    assert cfg.claude.setting_sources == ["user", "project", "local"]
    assert cfg.rlm.turn_budget == 40
    assert cfg.exs.worktree_root == ".claude/worktrees"
    assert cfg.sandbox.bwrap == "auto"
    assert cfg.workspace.path == os.getcwd()


def test_user_config_overrides_defaults(paths) -> None:
    user_toml = paths.config / "config.toml"
    user_toml.write_text(
        """
        [claude]
        model = "haiku"
        effort = "low"
        """,
        encoding="utf-8",
    )

    cfg = Config.load(paths)
    assert cfg.claude.model == "haiku"
    assert cfg.claude.effort == "low"
    # Untouched fields keep their packaged default.
    assert cfg.claude.permission_mode == "default"


def test_workspace_config_overrides_user_config(paths, tmp_path) -> None:
    user_toml = paths.config / "config.toml"
    user_toml.write_text('[claude]\nmodel = "haiku"\neffort = "low"\n', encoding="utf-8")

    workspace_dir = tmp_path / "myworkspace"
    (workspace_dir / ".lictor").mkdir(parents=True)
    (workspace_dir / ".lictor" / "config.toml").write_text(
        '[claude]\nmodel = "opus"\n', encoding="utf-8"
    )

    cfg = Config.load(paths, workspace_override=str(workspace_dir))
    assert cfg.claude.model == "opus"  # workspace wins over user
    assert cfg.claude.effort == "low"  # user config still applies where workspace is silent
    assert cfg.workspace.path == str(workspace_dir)


def test_cli_overrides_win_over_every_toml_file(paths, tmp_path) -> None:
    user_toml = paths.config / "config.toml"
    user_toml.write_text('[claude]\nmodel = "haiku"\n', encoding="utf-8")

    workspace_dir = tmp_path / "myworkspace"
    (workspace_dir / ".lictor").mkdir(parents=True)
    (workspace_dir / ".lictor" / "config.toml").write_text(
        '[claude]\nmodel = "opus"\n', encoding="utf-8"
    )

    cfg = Config.load(paths, workspace_override=str(workspace_dir))
    cfg.apply_cli_overrides(model="sonnet", effort="high", mode="acceptEdits")

    assert cfg.claude.model == "sonnet"
    assert cfg.claude.effort == "high"
    assert cfg.claude.permission_mode == "acceptEdits"


def test_resolve_cli_path_prefers_env_var(paths, monkeypatch) -> None:
    monkeypatch.setenv("LICTOR_CLAUDE", "/custom/claude")
    cfg = Config.load(paths)
    cfg.claude.cli_path = "/config/claude"
    assert cfg.resolve_cli_path() == "/custom/claude"


def test_resolve_cli_path_falls_back_to_config_value(paths, monkeypatch) -> None:
    monkeypatch.delenv("LICTOR_CLAUDE", raising=False)
    cfg = Config.load(paths)
    cfg.claude.cli_path = "/config/claude"
    assert cfg.resolve_cli_path() == "/config/claude"
