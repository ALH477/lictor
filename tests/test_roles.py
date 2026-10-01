"""roles.py: front-matter parsing, search-order override, and the summary
line fed into the system prompt append."""

from __future__ import annotations

from lictor import roles as roles_mod
from lictor.config import Config


def _write_role(directory, filename: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / filename).write_text(text, encoding="utf-8")


def test_load_builds_an_agent_definition_from_front_matter(paths, tmp_path) -> None:
    cfg = Config.load(paths, workspace_override=str(tmp_path / "ws"))
    _write_role(
        paths.config / "agents",
        "lexer.md",
        """---
name: lexer
description: Checks tokenization and source-policy behavior.
model: sonnet
tools: Read, Grep, Glob
max_turns: 8
effort: low
---

You own the lexer. Never invent an error code.
""",
    )

    result = roles_mod.load(cfg, paths)

    assert set(result) == {"lexer"}
    definition = result["lexer"]
    assert definition.description == "Checks tokenization and source-policy behavior."
    assert definition.prompt.strip() == "You own the lexer. Never invent an error code."
    assert definition.tools == ["Read", "Grep", "Glob"]
    assert definition.model == "sonnet"
    assert definition.maxTurns == 8
    assert definition.effort == "low"


def test_tools_accepts_bracketed_list_syntax(paths, tmp_path) -> None:
    cfg = Config.load(paths, workspace_override=str(tmp_path / "ws"))
    _write_role(
        paths.config / "agents",
        "bracketed.md",
        """---
description: Uses bracketed tool syntax.
tools: [Read, Grep]
---
Body.
""",
    )

    result = roles_mod.load(cfg, paths)
    assert result["bracketed"].tools == ["Read", "Grep"]


def test_name_defaults_to_filename_stem(paths, tmp_path) -> None:
    cfg = Config.load(paths, workspace_override=str(tmp_path / "ws"))
    _write_role(
        paths.config / "agents",
        "spec-guardian.md",
        """---
description: Checks a claim against the spec.
---
Body.
""",
    )

    result = roles_mod.load(cfg, paths)
    assert "spec-guardian" in result


def test_file_without_description_is_skipped(paths, tmp_path, caplog) -> None:
    cfg = Config.load(paths, workspace_override=str(tmp_path / "ws"))
    _write_role(
        paths.config / "agents",
        "no-description.md",
        """---
name: no-description
model: sonnet
---
Body.
""",
    )

    result = roles_mod.load(cfg, paths)
    assert result == {}


def test_file_without_front_matter_is_skipped(paths, tmp_path) -> None:
    cfg = Config.load(paths, workspace_override=str(tmp_path / "ws"))
    _write_role(paths.config / "agents", "plain.md", "Just a Markdown file, no fences.\n")

    result = roles_mod.load(cfg, paths)
    assert result == {}


def test_unrecognised_keys_are_ignored_not_fatal(paths, tmp_path) -> None:
    cfg = Config.load(paths, workspace_override=str(tmp_path / "ws"))
    _write_role(
        paths.config / "agents",
        "weird.md",
        """---
description: Has a key this parser does not know about.
mystery: something
---
Body.
""",
    )

    result = roles_mod.load(cfg, paths)
    assert "weird" in result


def test_workspace_role_overrides_user_role_by_name(paths, tmp_path) -> None:
    workspace = tmp_path / "ws"
    cfg = Config.load(paths, workspace_override=str(workspace))

    _write_role(
        paths.config / "agents",
        "shared.md",
        """---
name: shared
description: user-wide version.
---
user body.
""",
    )
    _write_role(
        workspace / ".lictor" / "agents",
        "shared.md",
        """---
name: shared
description: workspace version.
---
workspace body.
""",
    )

    result = roles_mod.load(cfg, paths)
    assert result["shared"].description == "workspace version."
    assert result["shared"].prompt.strip() == "workspace body."


def test_missing_agents_directories_return_empty_dict(paths, tmp_path) -> None:
    cfg = Config.load(paths, workspace_override=str(tmp_path / "ws"))
    assert roles_mod.load(cfg, paths) == {}


def test_summary_is_one_line_per_role() -> None:
    from claude_agent_sdk import AgentDefinition

    result = {
        "lexer": AgentDefinition(description="Checks tokens.\nmore detail", prompt="x"),
        "diag": AgentDefinition(description="Checks diagnostics.", prompt="y"),
    }
    text = roles_mod.summary(result)
    lines = text.splitlines()
    assert lines == ["lexer: Checks tokens.", "diag: Checks diagnostics."]


def test_summary_of_empty_roster_is_none_string() -> None:
    assert roles_mod.summary({}) == "none"


def test_example_role_files_ship_and_parse(paths, tmp_path) -> None:
    """The two example roles in lictor/prompts/roles/ are documentation, not
    auto-loaded (see the M5 brief) -- but they must still be well-formed
    role files if someone copies one into an agents/ directory."""
    from pathlib import Path

    examples_dir = Path(roles_mod.__file__).parent / "prompts" / "roles"
    assert (examples_dir / "spec-guardian.md").is_file()
    assert (examples_dir / "fixture.md").is_file()

    cfg = Config.load(paths, workspace_override=str(tmp_path / "ws"))
    target = paths.config / "agents"
    target.mkdir(parents=True, exist_ok=True)
    for name in ("spec-guardian.md", "fixture.md"):
        (target / name).write_text((examples_dir / name).read_text(encoding="utf-8"), encoding="utf-8")

    result = roles_mod.load(cfg, paths)
    assert set(result) == {"spec-guardian", "fixture"}
    assert result["spec-guardian"].tools is not None
    assert "mcp__exs__spec" in result["spec-guardian"].tools
    assert "mcp__exs__codes" in result["spec-guardian"].tools
    assert "mcp__exs__conformance" in result["fixture"].tools
