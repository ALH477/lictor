from __future__ import annotations

import json
from types import SimpleNamespace

from lictor import config as config_mod
from lictor.records import Records
from lictor.tools.resource import build
from lictor.vault import Vault


def _make_ctx(paths, workspace, cid="res-cid"):
    cfg = config_mod.Config()
    cfg.workspace.path = str(workspace)
    state = SimpleNamespace(workspace=workspace, cid=cid)
    return SimpleNamespace(cfg=cfg, state=state, paths=paths)


def _read_handler(ctx):
    tools = build(ctx)
    assert len(tools) == 1
    return tools[0].handler


async def test_workspace_read_file(paths, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hi there", encoding="utf-8")
    ctx = _make_ctx(paths, workspace)
    read = _read_handler(ctx)

    result = await read({"uri": "workspace:hello.txt"})
    assert result.get("is_error", False) is False
    assert result["content"][0]["text"] == "hi there"


async def test_workspace_list_directory(paths, tmp_path):
    workspace = tmp_path / "ws"
    (workspace / "sub").mkdir(parents=True)
    (workspace / "a.txt").write_text("a", encoding="utf-8")
    ctx = _make_ctx(paths, workspace)
    read = _read_handler(ctx)

    result = await read({"uri": "workspace:"})
    body = json.loads(result["content"][0]["text"])
    assert "a.txt" in body["entries"]
    assert "sub/" in body["entries"]


async def test_workspace_escape_is_refused(paths, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = _make_ctx(paths, workspace)
    read = _read_handler(ctx)

    result = await read({"uri": "workspace:../../etc/passwd"})
    assert result["is_error"] is True
    assert "escapes" in result["content"][0]["text"]


async def test_inference_scheme_reads_trace(paths, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = _make_ctx(paths, workspace)
    read = _read_handler(ctx)

    trace_dir = paths.data / "inferences"
    trace_dir.mkdir(parents=True, exist_ok=True)
    trace_doc = {"id": "inf-20260101-000000-abcd", "result": "hello"}
    (trace_dir / "inf-20260101-000000-abcd.json").write_text(json.dumps(trace_doc), encoding="utf-8")

    result = await read({"uri": "inference:inf-20260101-000000-abcd"})
    assert result.get("is_error", False) is False
    assert json.loads(result["content"][0]["text"]) == trace_doc

    missing = await read({"uri": "inference:does-not-exist"})
    assert missing["is_error"] is True


async def test_session_scheme_summarises_records(paths, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cid = "session-cid-1"
    records = Records(paths, cid)
    records.append("meta", {"cwd": str(workspace)})
    records.append("prompt", {"text": "hi"})
    records.append("assistant_text", {"text": "hello"})

    ctx = _make_ctx(paths, workspace, cid=cid)
    read = _read_handler(ctx)

    result = await read({"uri": f"session:{cid}"})
    body = json.loads(result["content"][0]["text"])
    assert body["cid"] == cid
    assert body["records"] == 3
    assert body["by_kind"]["prompt"] == 1
    assert body["meta"]["cwd"] == str(workspace)

    missing = await read({"uri": "session:no-such-cid"})
    assert missing["is_error"] is True


async def test_vault_current_scheme(paths, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    cid = "vault-cid-1"
    vault = Vault(paths, cid)
    vault.add("pending thing")

    ctx = _make_ctx(paths, workspace, cid=cid)
    read = _read_handler(ctx)

    result = await read({"uri": "vault:current"})
    body = json.loads(result["content"][0]["text"])
    assert body["cid"] == cid
    assert len(body["items"]) == 1
    assert body["summary"] is not None

    bad = await read({"uri": "vault:somethingelse"})
    assert bad["is_error"] is True


async def test_unknown_scheme_names_supported_schemes(paths, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = _make_ctx(paths, workspace)
    read = _read_handler(ctx)

    result = await read({"uri": "ftp:something"})
    assert result["is_error"] is True
    text = result["content"][0]["text"]
    for scheme in ("workspace", "inference", "session", "vault"):
        assert scheme in text

    no_colon = await read({"uri": "nocolonhere"})
    assert no_colon["is_error"] is True
