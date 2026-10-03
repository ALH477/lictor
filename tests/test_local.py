"""local.py / tools/local.py against a real HTTP server standing in for Ollama."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from lictor import config as config_mod
from lictor import local
from lictor.tools import ToolContext
from lictor.tools import load as load_tools


class _Fake(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        body = json.dumps({"models": [{"name": "exsecutor-3b:latest"}]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Fake.seen.append(req)
        if req["model"] == "boom":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"error":"model not found"}')
            return
        out = {"message": {"content": '{"x": 1}'}, "prompt_eval_count": 7, "eval_count": 3}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(out).encode())


@pytest.fixture
def server():
    _Fake.seen = []
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def _ctx(paths, tmp_path, url, **local_kw):
    cfg = config_mod.Config()
    cfg.local.enabled = True
    cfg.local.host = url
    for k, v in local_kw.items():
        setattr(cfg.local, k, v)
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "a.txt").write_text("hello")
    return SimpleNamespace(cfg=cfg, state=SimpleNamespace(workspace=ws, cid="c"), paths=paths, local_calls=0)


async def test_ask_roundtrip_trace_and_request_shape(paths, tmp_path, server):
    ctx = _ctx(paths, tmp_path, server)
    t = await local.ask(ctx, "what?", context=["a.txt"], schema={"type": "object"})
    assert t["unverified"] is True and t["structured_output"] == {"x": 1}
    req = _Fake.seen[0]
    assert req["model"] == "exsecutor-3b" and req["stream"] is False
    assert req["options"]["temperature"] == 0 and req["format"] == {"type": "object"}
    assert "hello" in req["messages"][1]["content"]
    assert json.loads((paths.data / "inferences" / f"{t['id']}.json").read_text())["result"] == '{"x": 1}'
    assert ctx.local_calls == 1


async def test_context_outside_workspace_refused_before_any_call(paths, tmp_path, server):
    ctx = _ctx(paths, tmp_path, server)
    (tmp_path / "secret.txt").write_text("nope")
    for bad in ("../secret.txt", str(tmp_path / "secret.txt")):
        with pytest.raises(local.LocalError, match="outside the workspace"):
            await local.ask(ctx, "p", context=[bad])
    assert _Fake.seen == [] and ctx.local_calls == 0


async def test_oversize_context_refused(paths, tmp_path, server):
    ctx = _ctx(paths, tmp_path, server)
    (tmp_path / "ws" / "big").write_bytes(b"x" * (local.MAX_CONTEXT_FILE_BYTES + 1))
    with pytest.raises(local.LocalError, match="refused rather than truncated"):
        await local.ask(ctx, "p", context=["big"])


async def test_disabled_and_cap(paths, tmp_path, server):
    ctx = _ctx(paths, tmp_path, server, enabled=False)
    with pytest.raises(local.LocalError, match="disabled"):
        await local.ask(ctx, "p")
    ctx = _ctx(paths, tmp_path, server, max_calls=1)
    await local.ask(ctx, "p")
    with pytest.raises(local.LocalError, match="cap"):
        await local.ask(ctx, "p")


async def test_http_error_and_unreachable_surface_as_localerror(paths, tmp_path, server):
    with pytest.raises(local.LocalError, match="model not found"):
        await local.ask(_ctx(paths, tmp_path, server, model="boom"), "p")
    with pytest.raises(local.LocalError, match="ollama serve"):
        await local.ask(_ctx(paths, tmp_path, "http://127.0.0.1:1"), "p")


async def test_status(paths, tmp_path, server):
    row = await local.status(_ctx(paths, tmp_path, server))
    assert row["reachable"] and row["model_present"]
    row = await local.status(_ctx(paths, tmp_path, "http://127.0.0.1:1"))
    assert row["reachable"] is False


def test_tools_register(paths):
    cfg = config_mod.Config()
    ctx = ToolContext(cfg=cfg, state=SimpleNamespace(workspace=paths.state, cid="c"), records=None, paths=paths)
    reg = load_tools(ctx, ("local",))
    assert sorted(reg.tools) == ["local.ask", "local.status"]
