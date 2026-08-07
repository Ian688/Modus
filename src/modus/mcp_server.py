"""MCP server: expose Modus's built-in tools to other AI agents (Phase A4).

The user's priority: "MCP is the top priority — it is most companies' consensus."
Modus already consumes MCP servers as a client; this module flips it around so
Modus can SERVE its built-in tools to another agent over the Model Context
Protocol (stdio transport first, mirroring the client's JSON-RPC loop).

Security posture (user-approved):

- **Read-only lens by default**: only tools that are safe + read_only +
  not requires_approval are exposed.  This is the blueprint T1 LENS — the
  remote agent can read files, search code, query system state, but cannot
  mutate anything.
- **`--allow-dangerous`** opts into the write/exec tools (bash, run_tests,
  spawn/kill_process, git writes, office writes).  Even then, a write-looking
  tool call is DENIED unless the call explicitly carries an approval token
  (headless approval is never automatic).
- **`--capabilities`** narrows to a subset of the exposed tools by capability
  class, reusing the deny-first ``capabilities_granted`` gate.
- **ToolContext** is constructed with the server's cwd and a deny-first
  approval callback so any tool that would ASK under normal HITL fails closed.

The stdio framing is newline-delimited JSON-RPC 2.0:
  → {"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}
  ← {"jsonrpc":"2.0","id":1,"result":{...}}
  → {"jsonrpc":"2.0","method":"notifications/initialized"}
  → {"jsonrpc":"2.0","id":2,"method":"tools/list"}
  → {"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":...,"arguments":{...}}}
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from modus.config import ModusConfig, load_config
from modus.tools.base import Tool, ToolContext, ToolResult
from modus.tools.builtins import get_builtin_tools
from modus.tools.registry import ToolRegistry

_PROTOCOL_VERSION = "2024-11-05"


def _is_read_only_lens(tool: Tool) -> bool:
    """Blueprint T1 LENS: safe + read-only + not approval-gated."""
    return tool.is_read_only and tool.danger_level == "safe" and not tool.requires_approval


def _tool_schema(tool: Tool) -> dict[str, Any]:
    """Convert a Modus tool to an MCP tool descriptor."""
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.parameters,
    }


def _build_registry(
    *, allow_dangerous: bool, capabilities: list[str] | None, cwd: str,
) -> ToolRegistry:
    """Build the tool registry this server exposes, filtered by policy."""
    from modus.tools.capabilities import capabilities_granted

    registry = ToolRegistry()
    for tool in get_builtin_tools():
        # Capability filter (deny-first): if a capability list is given, every
        # declared capability must be in it.
        if capabilities is not None and not capabilities_granted(tool.capabilities, capabilities):
            continue
        # Read-only lens default; dangerous tools only under --allow-dangerous.
        if _is_read_only_lens(tool):
            registry.register(tool)
        elif allow_dangerous:
            registry.register(tool)
        # else: skip (not exposed by default)
    return registry


def _make_context(cwd: str, config: ModusConfig) -> ToolContext:
    """Headless ToolContext: cwd anchored, approval deny-first."""
    async def _deny(_request: dict[str, Any]) -> str:
        return "deny"

    return ToolContext(
        cwd=cwd, config=config,
        approval_callback=_deny,
        workspace_root=cwd,
    )


class McpServer:
    """A stdio MCP server exposing a filtered Modus tool registry."""

    def __init__(
        self, *, cwd: str | None = None, allow_dangerous: bool = False,
        capabilities: list[str] | None = None, config: ModusConfig | None = None,
    ):
        self.cwd = cwd or os.getcwd()
        self.config = config or load_config(project_root=self.cwd)
        self.registry = _build_registry(
            allow_dangerous=allow_dangerous, capabilities=capabilities, cwd=self.cwd,
        )
        self._context = _make_context(self.cwd, self.config)

    def list_tool_descriptors(self) -> list[dict[str, Any]]:
        return [_tool_schema(self.registry.get(name)) for name in self.registry.list_names()
                if self.registry.get(name) is not None]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Execute one tool against the headless context, deny-first.

        The headless context has no human to approve an ASK/DENY decision, so
        every tool that would ask under normal HITL is refused.  Only
        auto-ALLOW (safe + read-only + not requires_approval) tools execute —
        unless ``--allow-dangerous`` was passed, which registers write/exec
        tools but STILL refuses them here unless they are auto-ALLOW.
        """
        from modus.policy.approval import ApprovalDecision, ApprovalPolicy

        tool = self.registry.get(name)
        if tool is None:
            return {"isError": True, "content": [{"type": "text", "text": f"tool not exposed: {name}"}]}
        decision = ApprovalPolicy(self.config.policy).evaluate(tool)
        if decision is not ApprovalDecision.ALLOW:
            return {
                "isError": True,
                "content": [{"type": "text",
                             "text": f"tool {name} requires human approval; headless MCP calls are denied."}],
            }
        try:
            result: ToolResult = await tool.execute(arguments, self._context)
        except Exception as exc:
            return {"isError": True, "content": [{"type": "text", "text": f"tool error: {exc}"}]}
        return {
            "isError": result.is_error,
            "content": [{"type": "text", "text": result.model_text()}],
        }

    async def _handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """Handle one JSON-RPC message; returns a response (or None for notify)."""
        method = str(msg.get("method") or "")
        msg_id = msg.get("id")
        params = msg.get("params") or {}
        if msg_id is None:
            # Notification — no response.
            return None

        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "modus", "version": "0.1.0"},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {"tools": self.list_tool_descriptors()},
            }
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            outcome = await self.call_tool(name, arguments)
            return {"jsonrpc": "2.0", "id": msg_id, "result": outcome}
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    async def run_stdio(self) -> None:
        """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader, loop=loop)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line = await reader.readline()
            if not line:
                break  # stdin closed
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = await self._handle(msg)
            if response is not None:
                # Direct write avoids the StreamWriterProtocol complexity; stdio
                # is a line protocol so a raw write + flush is race-free here.
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()


def mcp_serve(
    *, cwd: str | None = None, allow_dangerous: bool = False,
    capabilities: list[str] | None = None,
) -> None:
    """Run the stdio MCP server (blocking)."""
    asyncio.run(McpServer(
        cwd=cwd, allow_dangerous=allow_dangerous, capabilities=capabilities,
    ).run_stdio())


def build_http_app(
    *, cwd: str | None = None, allow_dangerous: bool = False,
    capabilities: list[str] | None = None,
) -> Any:
    """Build a FastAPI app serving the MCP server over Streamable HTTP.

    The same ``McpServer`` (registry filter + deny-first call gate) handles the
    JSON-RPC; only the transport changes.  ``POST /mcp`` accepts a JSON-RPC
    message and returns the response.  No session state is kept between
    requests (each call is independent), matching the read-only lens posture.
    """
    from fastapi import FastAPI, Body
    from fastapi.responses import JSONResponse

    server = McpServer(
        cwd=cwd, allow_dangerous=allow_dangerous, capabilities=capabilities,
    )
    app = FastAPI(title="Modus MCP", version="0.1.0")
    @app.get("/mcp")
    async def mcp_get():
        # Streamable HTTP handshake: the client probes with an empty GET to
        # discover the endpoint + capabilities.
        return JSONResponse({
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "modus", "version": "0.1.0"},
        })

    @app.post("/mcp")
    async def mcp_post(payload: dict = Body(...)):
        response = await server._handle(payload)
        if response is None:
            # Notification (no id) — 202 Accepted, no body.
            return JSONResponse({}, status_code=202)
        return JSONResponse(response)

    return app


def mcp_serve_http(
    *, cwd: str | None = None, allow_dangerous: bool = False,
    capabilities: list[str] | None = None, host: str = "127.0.0.1",
    port: int = 4000,
) -> None:
    """Run the MCP server over Streamable HTTP (blocking)."""
    import uvicorn

    app = build_http_app(
        cwd=cwd, allow_dangerous=allow_dangerous, capabilities=capabilities,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
