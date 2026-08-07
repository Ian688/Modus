from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Any

from modus.mcp_client import McpManager, McpServerConfig
from modus.tools.base import Tool, ToolContext, ToolResult, object_schema


@dataclass(frozen=True, slots=True)
class ExtensionDefinition:
    """A browser-safe extension record; secrets are intentionally excluded."""

    id: str
    kind: str
    name: str
    enabled: bool
    summary: str
    source: str
    status: str = "available"

    def to_wire(self) -> dict[str, str | bool]:
        return {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "enabled": self.enabled,
            "summary": self.summary,
            "source": self.source,
            "status": self.status,
        }


def builtin_extensions() -> list[ExtensionDefinition]:
    """Capabilities genuinely wired into the current executable registry."""
    return [
        ExtensionDefinition("builtin.tools", "builtin", "内置工具", True, "文件、终端与受控执行工具", "Modus", "active"),
        ExtensionDefinition("builtin.skills", "skill", "常用提示模板", True, "代码审查、架构设计、测试覆盖", "Modus", "active"),
    ]


class ExtensionRegistry:
    """Lifecycle boundary for Skills, MCP and plugins."""

    def __init__(self, mcp_manager: McpManager | None = None) -> None:
        self._items = {item.id: item for item in builtin_extensions()}
        self._mcp = mcp_manager

    def attach_mcp(self, mcp_manager: McpManager) -> None:
        self._mcp = mcp_manager

    def list_public(self) -> list[dict[str, str | bool]]:
        result = [item.to_wire() for item in self._items.values()]
        if self._mcp:
            for cfg in self._mcp.list_configs():
                status = "connected" if self._mcp.is_connected(str(cfg["name"])) else "configured"
                result.append({
                    "id": f"mcp.{cfg['name']}",
                    "kind": "mcp",
                    "name": cfg["name"],
                    "enabled": cfg.get("enabled", True),
                    "summary": f"{cfg.get('transport', 'stdio')} MCP server",
                    "source": cfg.get("command") or cfg.get("url", ""),
                    "status": status,
                })
        return result

    def add_placeholder(self, *, kind: str, name: str, summary: str) -> ExtensionDefinition:
        if kind not in {"skill", "mcp", "plugin"}:
            raise ValueError("extension kind must be skill, mcp, or plugin")
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("extension name is required")
        identifier = f"pending.{kind}.{cleaned.lower().replace(' ', '-')[:40]}"
        item = ExtensionDefinition(identifier, kind, cleaned, False, summary.strip() or "待配置", "本机", "not_connected")
        self._items[identifier] = item
        return item

    async def contribute_tools(self, _config: Any, _cwd: str) -> list[Tool]:
        """Return connected MCP tools under collision-safe public names."""
        tools: list[Tool] = []
        if self._mcp:
            mcp_tools = self._mcp.list_tools()
            for mt in mcp_tools:
                tool = Tool(
                    name=self._mcp_tool_name(mt.server_name, mt.name),
                    description=f"[MCP:{mt.server_name}] {mt.description}",
                    parameters=mt.input_schema or {"type": "object", "properties": {}},
                    handler=self._make_mcp_handler(mt.server_name, mt.name),
                    is_read_only=False,
                    is_concurrency_safe=False,
                    danger_level="medium",
                    requires_approval=True,
                    capabilities=("agent",),
                )
                tools.append(tool)
        await asyncio.sleep(0)
        return tools

    def _make_mcp_handler(self, server_name: str, tool_name: str):
        """Create an async handler that delegates to the MCP client."""

        async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
            if not self._mcp:
                return ToolResult(content="MCP manager not available", is_error=True)
            try:
                result = await self._mcp.call_tool(
                    tool_name, args, server_name=server_name,
                )
                content = result.get("content", "")
                is_error = result.get("is_error", False)
                return ToolResult(content=str(content), is_error=is_error)
            except Exception as exc:
                return ToolResult(content=f"MCP error: {exc}", is_error=True)

        return handler

    @staticmethod
    def _tool_slug(value: str, limit: int) -> str:
        """Create an LLM-provider-safe segment without exposing raw syntax."""
        slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value)).strip("_").lower()
        return slug[:limit] or "unnamed"

    @classmethod
    def _mcp_tool_name(cls, server_name: str, tool_name: str) -> str:
        server_digest = hashlib.sha256(str(server_name).encode()).hexdigest()[:6]
        tool_digest = hashlib.sha256(
            f"{server_name}\0{tool_name}".encode(),
        ).hexdigest()[:6]
        server = cls._tool_slug(server_name, 16)
        tool = cls._tool_slug(tool_name, 22)
        return f"mcp__{server}_{server_digest}__{tool}_{tool_digest}"
