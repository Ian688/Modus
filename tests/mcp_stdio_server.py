"""A minimal MCP stdio server used to exercise the real subprocess lifecycle.

Implements just enough JSON-RPC over newline-delimited stdio for
``modus.mcp_client.McpClient``: initialize, notifications/initialized,
tools/list and tools/call.  Run with ``python -m tests.mcp_stdio_server`` or
as the command of an ``McpServerConfig``.
"""

from __future__ import annotations

import json
import sys


TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two integers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer"},
                "b": {"type": "integer"},
            },
            "required": ["a", "b"],
        },
    },
]


def _handle(method: str, params: dict) -> dict:
    if method == "initialize":
        return {"protocolVersion": "2024-11-05", "serverInfo": {"name": "modus-test", "version": "0.0.1"}}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments", {}) or {}
        if name == "echo":
            return {"content": [{"type": "text", "text": str(arguments.get("text", ""))}]}
        if name == "add":
            total = int(arguments.get("a", 0)) + int(arguments.get("b", 0))
            return {"content": [{"type": "text", "text": str(total)}]}
        return {"content": [], "isError": True}
    return {"content": [], "isError": True}


def main() -> None:
    for line in sys.stdin:
        raw = line.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if "method" not in msg:
            continue
        method = msg["method"]
        if method == "notifications/initialized":
            continue
        result = _handle(method, msg.get("params", {}))
        response = {"jsonrpc": "2.0", "id": msg.get("id"), "result": result}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
