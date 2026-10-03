"""ollama.py: backend env, preflight, and its wiring into options/config."""

from __future__ import annotations

import io
import json

import pytest

from lictor import ollama
from lictor.config import Config
from lictor.options import State, build_options


def _cfg(paths, **kw) -> Config:
    cfg = Config.load(paths)
    cfg.apply_cli_overrides(**kw)
    return cfg


def test_off_by_default_adds_no_env(paths) -> None:
    cfg = Config.load(paths)
    assert ollama.backend_env(cfg) == {}
    state = State(cwd=paths.state, workspace=paths.state, active_worktree=None, cid="c")
    assert build_options(cfg, state, fresh_cid="c").env == {"MCP_TIMEOUT": "120000"}


def test_cli_flag_enables_and_sets_model(paths) -> None:
    cfg = _cfg(paths, ollama="qwen3-coder")
    env = ollama.backend_env(cfg)
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert env["ANTHROPIC_API_KEY"] == ""
    for alias in ("OPUS", "SONNET", "HAIKU"):
        assert env[f"ANTHROPIC_DEFAULT_{alias}_MODEL"] == "qwen3-coder"
    state = State(cwd=paths.state, workspace=paths.state, active_worktree=None, cid="c")
    opts = build_options(cfg, state, fresh_cid="c")
    assert opts.model == "qwen3-coder"
    assert opts.env["ANTHROPIC_BASE_URL"] == "http://localhost:11434"


def test_bare_flag_keeps_configured_model(paths) -> None:
    (paths.config / "config.toml").write_text('[ollama]\nmodel = "llama3.1"\nhost = "http://gpu:11434/"\n')
    cfg = _cfg(paths, ollama="")
    assert cfg.ollama.enabled and cfg.ollama.model == "llama3.1"
    assert ollama.backend_env(cfg)["ANTHROPIC_BASE_URL"] == "http://gpu:11434"


def test_model_override_wins_over_ollama_model(paths) -> None:
    cfg = _cfg(paths, ollama="a", model="b")
    assert ollama.effective_model(cfg) == "b"


def _serve(monkeypatch, models) -> None:
    body = json.dumps({"models": [{"name": m} for m in models]}).encode()
    monkeypatch.setattr(ollama.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(body))


def test_preflight_ok_and_latest_tag(paths, monkeypatch) -> None:
    _serve(monkeypatch, ["llama3:latest"])
    assert "llama3" in ollama.preflight(_cfg(paths, ollama="llama3"))


def test_preflight_missing_model(paths, monkeypatch) -> None:
    _serve(monkeypatch, ["other:7b"])
    with pytest.raises(ollama.OllamaError, match="ollama pull"):
        ollama.preflight(_cfg(paths, ollama="llama3"))


def test_preflight_no_model_configured(paths) -> None:
    with pytest.raises(ollama.OllamaError, match="no Ollama model"):
        ollama.preflight(_cfg(paths, ollama=""))


def test_preflight_unreachable(paths, monkeypatch) -> None:
    def boom(*a, **k):
        raise ollama.urllib.error.URLError("refused")

    monkeypatch.setattr(ollama.urllib.request, "urlopen", boom)
    with pytest.raises(ollama.OllamaError, match="ollama serve"):
        ollama.preflight(_cfg(paths, ollama="x"))
