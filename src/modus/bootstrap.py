from __future__ import annotations

from modus.config import ModusConfig
from modus.extensions import ExtensionRegistry
from modus.tools import ToolRegistry, get_builtin_tools

async def build_tool_registry(
    *,
    config: ModusConfig,
    cwd: str,
    extension_registry: ExtensionRegistry | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()
    builtin = get_builtin_tools()
    allowed = set(config.tools.enabled or ())
    blocked = set(config.tools.disabled or ())
    if allowed:
        builtin = [tool for tool in builtin if tool.name in allowed]
    if blocked:
        builtin = [tool for tool in builtin if tool.name not in blocked]
    registry.register_all(builtin)
    # Desktop supplies its process-owned registry so connected MCP servers are
    # part of the actual Agent tool catalog. CLI callers keep an isolated
    # registry unless they explicitly provide another lifecycle owner.
    extensions = extension_registry or ExtensionRegistry()
    contributed = await extensions.contribute_tools(config, cwd)
    if blocked:
        contributed = [tool for tool in contributed if tool.name not in blocked]
    registry.register_all(contributed)
    return registry
