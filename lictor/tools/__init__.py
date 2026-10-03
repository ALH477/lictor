"""The tool registry: lictor's own tools, grouped by namespace.

An Anthropic tool name must match ``^[a-zA-Z0-9_-]+$``, so a tool cannot
be called ``exs.build`` on the wire. lictor therefore runs **one
in-process MCP server per namespace**: ``exs.build`` is registered here
under the dotted name, reaches the model as ``mcp__exs__build``, and is
spoken of as ``exs.build`` everywhere a person reads it.

Each namespace lives in its own module (``lictor/tools/exs.py`` and so
on) and exposes exactly two things:

    NAMESPACE: str
    def build(ctx: ToolContext) -> list[SdkMcpTool]: ...

``build`` returns the objects produced by the SDK's ``@tool`` decorator.
Nothing else in the module is imported, so the namespaces stay
independent of one another.

The registry keeps the ``SdkMcpTool`` objects themselves, not bound
handler functions. ``create_sdk_mcp_server`` closes over those objects
and reads ``tool_def.handler`` at call time, which is what lets
``self.redefine`` swap a handler on a live session. The wire tool list
and the schemas are fixed when the server is created, so a redefine may
change behaviour but not a schema, and may not add a tool.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: Namespace modules the registry will try to load, in order. A module
#: that does not exist yet is skipped, so waves can land one at a time.
NAMESPACES = ("exs", "self_", "py", "rlm", "local", "resource")

#: The namespace a module is addressed by, where it differs from the
#: module name. ``self`` is a Python keyword, so the module is ``self_``.
MODULE_NAMESPACE = {"self_": "self"}

_WIRE_NAME = re.compile(r"^[a-zA-Z0-9_-]+$")


@dataclass
class ToolContext:
    """Everything a tool handler is allowed to reach.

    Handlers run on the asyncio loop thread. Anything that blocks must
    go through ``asyncio.create_subprocess_exec`` with a timeout: the
    same loop services approval requests and hooks, so a blocking
    handler stalls the session's ability to ask permission.
    """

    cfg: Any
    state: Any
    records: Any
    paths: Any
    app: Any = None
    registry: ToolRegistry | None = None
    image: Any = None
    workers: Any = None
    budget: Any = None
    local_calls: int = 0


@dataclass
class ToolRegistry:
    """Dotted name -> ``SdkMcpTool``, plus the per-namespace servers."""

    tools: dict[str, Any] = field(default_factory=dict)
    _servers: dict[str, Any] = field(default_factory=dict)

    def register(self, namespace: str, sdk_tool: Any) -> str:
        name = sdk_tool.name
        if not _WIRE_NAME.match(name):
            raise ValueError(
                f"tool name {name!r} is not valid on the wire; "
                "a namespace is a server, not a dotted name"
            )
        dotted = f"{namespace}.{name}"
        if dotted in self.tools:
            raise ValueError(f"tool {dotted} is already registered")
        self.tools[dotted] = sdk_tool
        self._servers.clear()
        return dotted

    def rebind(self, dotted: str, handler: Callable[..., Any]) -> Any:
        """Point a registered tool at a new handler, live.

        Returns the previous handler so a discard can put it back. The
        schema is not touched; see the module docstring.
        """
        sdk_tool = self.tools[dotted]
        previous = sdk_tool.handler
        sdk_tool.handler = handler
        return previous

    def namespaces(self) -> list[str]:
        seen: list[str] = []
        for dotted in self.tools:
            ns = dotted.split(".", 1)[0]
            if ns not in seen:
                seen.append(ns)
        return seen

    def servers(self) -> dict[str, Any]:
        """``mcp_servers`` for ``ClaudeAgentOptions``, one per namespace.

        Built once and cached: the server closes over the tool objects,
        so a later ``rebind`` is visible without rebuilding, but a
        ``register`` after a server exists would not be, and clears the
        cache instead.
        """
        if not self._servers:
            from claude_agent_sdk import create_sdk_mcp_server

            from lictor import __version__

            for ns in self.namespaces():
                in_ns = [t for d, t in self.tools.items() if d.split(".", 1)[0] == ns]
                self._servers[ns] = create_sdk_mcp_server(ns, __version__, in_ns)
        return dict(self._servers)

    def tool_ids(self) -> list[str]:
        """Wire ids, for ``allowed_tools``."""
        return [f"mcp__{d.split('.', 1)[0]}__{d.split('.', 1)[1]}" for d in self.tools]

    def wire_id(self, dotted: str) -> str:
        ns, name = dotted.split(".", 1)
        return f"mcp__{ns}__{name}"


def load(ctx: ToolContext, namespaces: tuple[str, ...] = NAMESPACES) -> ToolRegistry:
    """Import every available namespace module and register its tools."""
    registry = ToolRegistry()
    ctx.registry = registry
    for module_name in namespaces:
        try:
            module = importlib.import_module(f"lictor.tools.{module_name}")
        except ModuleNotFoundError:
            continue
        namespace = MODULE_NAMESPACE.get(module_name, getattr(module, "NAMESPACE", module_name))
        for sdk_tool in module.build(ctx):
            registry.register(namespace, sdk_tool)
    return registry
