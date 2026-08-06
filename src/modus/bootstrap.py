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
    registry.register_all(get_builtin_tools())
    # Desktop supplies its process-owned registry so connected MCP servers are
    # part of the actual Agent tool catalog. CLI callers keep an isolated
    # registry unless they explicitly provide another lifecycle owner.
    extensions = extension_registry or ExtensionRegistry()
    registry.register_all(await extensions.contribute_tools(config, cwd))
    return registry
