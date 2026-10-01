"""recovery.py: the boot-marker protocol's recovery decision, sanitize()'s
redactions, and capture_crash() never writing an env value."""

from __future__ import annotations

import json
from pathlib import Path

from lictor import recovery


def test_decide_boot_is_recovery_after_a_stale_starting_marker(paths):
    recovery.write_starting(paths, gen=0)

    decision = recovery.decide_boot(paths, forced_recovery=False)

    assert decision.recovery is True
    assert "startup" in decision.reason


def test_decide_boot_is_normal_after_a_clean_clear(paths):
    recovery.write_starting(paths, gen=0)
    recovery.mark_booted(paths)
    recovery.clear(paths)

    decision = recovery.decide_boot(paths, forced_recovery=False)

    assert decision.recovery is False
    assert decision.reason == "clean"


def test_decide_boot_is_normal_after_mark_booted_without_clear(paths):
    # phase moved from "starting" to "booted" -- a live, still-running
    # process, or one that exited without cleaning up; either way it is
    # not "died during startup".
    recovery.write_starting(paths, gen=0)
    recovery.mark_booted(paths)

    decision = recovery.decide_boot(paths, forced_recovery=False)

    assert decision.recovery is False


def test_decide_boot_forced_recovery(paths):
    recovery.write_starting(paths, gen=0)
    recovery.mark_booted(paths)
    recovery.clear(paths)

    decision = recovery.decide_boot(paths, forced_recovery=True)

    assert decision.recovery is True
    assert "forced" in decision.reason


def test_decide_boot_surfaces_the_latest_crash_without_forcing_recovery(paths):
    recovery.write_starting(paths, gen=0)
    recovery.mark_booted(paths)
    try:
        raise RuntimeError("after boot")
    except RuntimeError as exc:
        recovery.capture_crash(paths, exc)
    recovery.clear(paths)

    decision = recovery.decide_boot(paths, forced_recovery=False)

    assert decision.recovery is False
    assert decision.crash_path is not None
    assert decision.crash_summary is not None


# -- sanitize -----------------------------------------------------------


def test_sanitize_collapses_home_store_paths_and_tokens():
    home = str(Path.home())
    token = "sk-" + ("A" * 40)
    text = f"File at {home}/Documents/x.py, loaded from /nix/store/{'a' * 32}-glibc-2.38, key={token}"

    sanitized = recovery.sanitize(text)

    assert home not in sanitized
    assert "~" in sanitized
    assert "/nix/store/" not in sanitized
    assert "<store>/" in sanitized
    assert token not in sanitized
    assert "<redacted>" in sanitized


def test_sanitize_is_idempotent_on_already_clean_text():
    text = "a perfectly ordinary line of traceback, no secrets here"
    assert recovery.sanitize(text) == text


# -- capture_crash ------------------------------------------------------


def test_capture_crash_never_writes_an_env_value(paths, monkeypatch):
    secret_value = "lictor-test-secret-9f3a7c21"
    monkeypatch.setenv("LICTOR_TEST_SECRET", secret_value)

    try:
        raise ValueError("boom for capture_crash")
    except ValueError as exc:
        path = recovery.capture_crash(paths, exc)

    assert path.is_file()
    raw = path.read_text(encoding="utf-8")
    assert secret_value not in raw

    data = json.loads(raw)
    assert "boom for capture_crash" in data["traceback"]
    assert "lictor" in data
    assert "sdk" in data
    assert "log_tail" in data
    assert secret_value not in json.dumps(data)


def test_capture_crash_sanitizes_the_home_directory_in_the_traceback(paths):
    home = str(Path.home())

    def _raise_with_home_in_message():
        raise RuntimeError(f"could not read {home}/secret.txt")

    try:
        _raise_with_home_in_message()
    except RuntimeError as exc:
        path = recovery.capture_crash(paths, exc)

    raw = path.read_text(encoding="utf-8")
    assert home not in raw
    assert "~/secret.txt" in raw
