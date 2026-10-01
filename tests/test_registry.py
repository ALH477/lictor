"""The tool registry seam: wire names, live rebinding, incremental waves."""

from __future__ import annotations

import pytest
from claude_agent_sdk import tool

from lictor.tools import ToolContext, ToolRegistry, load


def _tool(name: str, text: str = "ok"):
    @tool(name, f"test tool {name}", {})
    async def handler(args):  # pragma: no cover - replaced in rebind test
        return {"content": [{"type": "text", "text": text}]}

    return handler


def test_register_builds_dotted_and_wire_names():
    reg = ToolRegistry()
    assert reg.register("exs", _tool("build")) == "exs.build"
    assert reg.wire_id("exs.build") == "mcp__exs__build"
    assert reg.tool_ids() == ["mcp__exs__build"]


def test_a_dotted_tool_name_is_refused():
    reg = ToolRegistry()
    with pytest.raises(ValueError, match="not valid on the wire"):
        reg.register("exs", _tool("exs.build"))


def test_duplicate_registration_is_refused():
    reg = ToolRegistry()
    reg.register("exs", _tool("build"))
    with pytest.raises(ValueError, match="already registered"):
        reg.register("exs", _tool("build"))


def test_rebind_swaps_the_handler_and_returns_the_old_one():
    reg = ToolRegistry()
    reg.register("exs", _tool("build"))
    original = reg.tools["exs.build"].handler

    async def replacement(args):
        return {"content": [{"type": "text", "text": "redefined"}]}

    assert reg.rebind("exs.build", replacement) is original
    assert reg.tools["exs.build"].handler is replacement
    reg.rebind("exs.build", original)
    assert reg.tools["exs.build"].handler is original


def test_servers_are_one_per_namespace():
    reg = ToolRegistry()
    reg.register("exs", _tool("build"))
    reg.register("exs", _tool("test"))
    reg.register("py", _tool("eval"))
    servers = reg.servers()
    assert sorted(servers) == ["exs", "py"]
    assert reg.namespaces() == ["exs", "py"]


def test_a_rebind_is_visible_through_an_existing_server():
    reg = ToolRegistry()
    reg.register("exs", _tool("build"))
    server = reg.servers()["exs"]

    async def replacement(args):
        return {"content": [{"type": "text", "text": "redefined"}]}

    reg.rebind("exs.build", replacement)
    assert reg.servers()["exs"] is server
    assert reg.tools["exs.build"].handler is replacement


def test_load_skips_namespace_modules_that_do_not_exist_yet(paths):
    reg = load(ToolContext(cfg=None, state=None, records=None, paths=paths),
               namespaces=("not_a_wave_yet",))
    assert reg.tools == {}
