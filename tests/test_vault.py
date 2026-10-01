from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from lictor.vault import Vault

_REPO_ROOT = Path(__file__).resolve().parents[1]


def test_add_inflight_done_lifecycle(paths):
    vault = Vault(paths, "cid-1")
    assert vault.items() == []

    item_id = vault.add("hello", to="lexer")
    items = vault.items()
    assert len(items) == 1
    assert items[0]["id"] == item_id
    assert items[0]["status"] == "queued"
    assert items[0]["to"] == "lexer"
    assert items[0]["attempts"] == 0

    vault.mark_inflight(item_id)
    assert vault.items()[0]["status"] == "inflight"
    assert vault.items()[0]["attempts"] == 1

    vault.done(item_id)
    assert vault.items() == []


def test_restore_order_is_insertion_order(paths):
    vault = Vault(paths, "cid-order")
    ids = [vault.add(f"item-{i}") for i in range(5)]
    vault.mark_inflight(ids[2])
    assert [item["id"] for item in vault.restore_order()] == ids


def test_done_unknown_id_raises(paths):
    vault = Vault(paths, "cid-2")
    try:
        vault.done("no-such-id")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_atomic_rewrite_leaves_valid_json(paths):
    vault = Vault(paths, "cid-3")
    for i in range(20):
        item_id = vault.add(f"text-{i}")
        if i % 2 == 0:
            vault.mark_inflight(item_id)

    raw = (paths.state / "vault" / "cid-3.json").read_text(encoding="utf-8")
    doc = json.loads(raw)  # must parse: no partial/tmp content ever lands at the real path
    assert doc["cid"] == "cid-3"
    assert len(doc["items"]) == 20
    # no leftover .tmp-* files
    leftovers = list((paths.state / "vault").glob("cid-3.json.tmp-*"))
    assert leftovers == []


def test_summary(paths):
    vault = Vault(paths, "cid-4")
    assert vault.summary() is None
    vault.add("x" * 100)
    summary = vault.summary()
    assert summary is not None
    assert "1 pending item" in summary


def test_discard_all_archives_and_empties(paths):
    vault = Vault(paths, "cid-5")
    vault.add("one")
    vault.add("two")
    archive_path = vault.discard_all()
    assert archive_path is not None
    assert archive_path.exists()
    archived = json.loads(archive_path.read_text(encoding="utf-8"))
    assert len(archived["items"]) == 2

    assert vault.items() == []
    assert vault.discard_all() is None  # nothing left to discard


_CHILD_SRC = """
import sys
sys.path.insert(0, {repo!r})
from lictor.config import Paths
from lictor.vault import Vault

paths = Paths.resolve()
vault = Vault(paths, {cid!r})
item_id = vault.add("don't lose me")
vault.mark_inflight(item_id)
print(item_id, flush=True)
import time
time.sleep(60)
"""


def test_crash_survival_real_child_process(paths, tmp_path):
    """A real child process adds an item, marks it inflight, reports the
    item id, and is then SIGKILLed before it ever calls done(). A fresh
    Vault opened by this (the parent) process afterwards must still see
    that item, status inflight -- proving the on-disk write survives a
    hard kill between mark_inflight and done."""
    cid = "crash-cid"
    child_script = tmp_path / "child_vault.py"
    child_script.write_text(_CHILD_SRC.format(repo=str(_REPO_ROOT), cid=cid), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(child_script)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        line = proc.stdout.readline()
        item_id = line.strip()
        assert item_id, "child never reported an item id before being killed"

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    fresh = Vault(paths, cid)
    pending = fresh.restore_order()
    assert any(item["id"] == item_id and item["status"] == "inflight" for item in pending)
