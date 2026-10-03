"""Local Ollama as the inference backend.

lictor still drives the local ``claude`` binary through the Agent SDK; it
does not speak to any model API itself. Ollama serves an Anthropic-
compatible Messages endpoint at its own root (``http://localhost:11434``),
so pointing the binary at it takes only environment variables, set on the
subprocess through ``ClaudeAgentOptions(env=...)``:

* ``ANTHROPIC_BASE_URL`` -- the Ollama host;
* ``ANTHROPIC_AUTH_TOKEN`` -- a placeholder (Ollama ignores it, the binary
  insists on one);
* ``ANTHROPIC_API_KEY`` -- emptied, so a key left in the operator's shell
  can never be sent to a local server;
* ``ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL`` and
  ``ANTHROPIC_SMALL_FAST_MODEL`` -- the one Ollama model, so every alias
  lictor uses elsewhere (``rlm``'s ``haiku`` default, a role's ``sonnet``,
  the binary's own background calls) resolves to something Ollama serves;
* ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` -- no telemetry/update
  pings to Anthropic from an otherwise local session.

:func:`preflight` is the one place lictor itself touches the network, and
only against the configured Ollama host.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

DEFAULT_HOST = "http://localhost:11434"


class OllamaError(Exception):
    """Ollama is unreachable, or does not have the requested model."""


def effective_model(cfg: Any) -> str:
    """The model the session runs on: ``[claude].model`` (set by ``--model``
    or ``/model``) if present, else ``[ollama].model``."""
    return cfg.claude.model or cfg.ollama.model


def backend_env(cfg: Any) -> dict[str, str]:
    """Environment overrides for the ``claude`` subprocess; ``{}`` unless
    ``[ollama].enabled``."""
    o = cfg.ollama
    if not o.enabled:
        return {}
    model = effective_model(cfg)
    env = {
        "ANTHROPIC_BASE_URL": o.host.rstrip("/"),
        "ANTHROPIC_AUTH_TOKEN": "ollama",
        "ANTHROPIC_API_KEY": "",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    if model:
        for key in (
            "ANTHROPIC_DEFAULT_OPUS_MODEL",
            "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            "ANTHROPIC_SMALL_FAST_MODEL",
        ):
            env[key] = model
    return env


def list_models(host: str, timeout: float = 3.0) -> list[str]:
    """Names of the models the Ollama server at `host` has pulled."""
    url = host.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - operator-configured host
            data = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise OllamaError(f"cannot reach Ollama at {host} ({exc}); is `ollama serve` running?") from exc
    return [m.get("name", "") for m in data.get("models", []) if isinstance(m, dict)]


def _has(models: list[str], wanted: str) -> bool:
    # `llama3` means `llama3:latest` to Ollama.
    return wanted in models or (":" not in wanted and f"{wanted}:latest" in models)


def preflight(cfg: Any) -> str:
    """Check the server answers and has the model; return a one-line status.

    Raises :class:`OllamaError` with an actionable message otherwise."""
    model = effective_model(cfg)
    if not model:
        raise OllamaError(
            "no Ollama model set: pass --ollama MODEL, or set [ollama].model in config.toml"
        )
    models = list_models(cfg.ollama.host)
    if not _has(models, model):
        have = ", ".join(models) or "none"
        raise OllamaError(f"Ollama at {cfg.ollama.host} has no model {model!r} (pulled: {have}); "
                          f"run `ollama pull {model}`")
    return f"ollama {cfg.ollama.host} · model {model}"
