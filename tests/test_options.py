"""options.py: scrub_environ's keep-list, and build_options' resume XOR
session_id and verbatim_prompts guard."""

from __future__ import annotations

import pytest

from lictor.config import Config
from lictor.options import State, build_options, scrub_environ


def test_scrub_removes_claude_code_prefixed_keys() -> None:
    env = {
        "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
        "CLAUDE_CODE_FOO": "bar",
        "CLAUDECODE": "1",
        "CLAUDE_PID": "123",
        "CLAUDE_EFFORT": "high",
        "ANTHROPIC_API_KEY": "sk-whatever",
        "OTHER_VAR": "untouched",
    }
    removed = scrub_environ(env)

    assert set(removed) == {
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_FOO",
        "CLAUDECODE",
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
        "ANTHROPIC_API_KEY",
    }
    assert env == {"OTHER_VAR": "untouched"}


def test_scrub_keeps_oauth_token_and_config_dir() -> None:
    env = {
        "CLAUDE_CODE_OAUTH_TOKEN": "token-value",
        "CLAUDE_CONFIG_DIR": "/some/dir",
        "CLAUDE_CODE_OTHER": "gone",
    }
    removed = scrub_environ(env)

    assert "CLAUDE_CODE_OTHER" in removed
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in removed
    assert "CLAUDE_CONFIG_DIR" not in removed
    assert env == {"CLAUDE_CODE_OAUTH_TOKEN": "token-value", "CLAUDE_CONFIG_DIR": "/some/dir"}


def _state(paths, cid="cid-opts") -> State:
    return State(cwd=paths.state, workspace=paths.state, active_worktree=None, cid=cid)


def test_build_options_sets_session_id_for_a_fresh_conversation(paths) -> None:
    cfg = Config.load(paths)
    state = _state(paths)

    opts = build_options(cfg, state, fresh_cid=state.cid)

    assert opts.session_id == state.cid
    assert opts.resume is None


def test_build_options_sets_resume_not_session_id(paths) -> None:
    cfg = Config.load(paths)
    state = _state(paths)

    opts = build_options(cfg, state, resume="prior-session-id")

    assert opts.resume == "prior-session-id"
    assert opts.session_id is None


def test_build_options_rejects_both_resume_and_fresh_cid(paths) -> None:
    cfg = Config.load(paths)
    state = _state(paths)

    with pytest.raises(ValueError):
        build_options(cfg, state, resume="prior", fresh_cid="fresh")


def test_build_options_sets_verbatim_prompts_when_the_sdk_supports_it(paths) -> None:
    from claude_agent_sdk import ClaudeAgentOptions

    cfg = Config.load(paths)
    state = _state(paths)
    opts = build_options(cfg, state, fresh_cid=state.cid)

    if hasattr(ClaudeAgentOptions, "verbatim_prompts"):
        assert opts.verbatim_prompts is True
    else:
        assert not hasattr(opts, "verbatim_prompts") or opts.verbatim_prompts is False


def test_build_options_base_allowed_tools_and_system_prompt(paths) -> None:
    cfg = Config.load(paths)
    state = _state(paths)
    opts = build_options(cfg, state, fresh_cid=state.cid)

    for name in ("Read", "Glob", "Grep", "TodoWrite", "Agent", "WebFetch"):
        assert name in opts.allowed_tools

    assert opts.system_prompt["type"] == "preset"
    assert opts.system_prompt["preset"] == "claude_code"
    assert "lictor" in opts.system_prompt["append"]
    assert str(state.workspace) in opts.system_prompt["append"]
