"""End-to-end MCP stdio lifecycle: real subprocess → discover → call.

Unlike the unit tests that mock ``_connect_stdio``, these tests start a real
Python subprocess speaking newline-delimited JSON-RPC and verify the full
McpClient → Tool adapter → execution path.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from modus.mcp_client import McpClient, McpServerConfig


SERVER_MODULE = str(Path(__file__).resolve().parent / "mcp_stdio_server.py")


def _stdio_config(name: str = "test-server") -> McpServerConfig:
    return McpServerConfig(
        name=name,
        transport="stdio",
        command=sys.executable,
        args=["-B", SERVER_MODULE],
    )


@pytest.mark.asyncio
async def test_connect_discovers_tools_and_calls_them_over_real_subprocess():
    client = McpClient(_stdio_config())
    try:
        await client.connect()

        assert client._connected is True
        tools = client.tools
        names = {tool.name for tool in tools}
        assert names == {"echo", "add"}

        echo = await client.call_tool("echo", {"text": "hello modus"})
        assert echo["content"] == "hello modus"
        assert echo["is_error"] is False

        added = await client.call_tool("add", {"a": 2, "b": 3})
        assert added["content"] == "5"
    finally:
        await client.disconnect()

    # Subprocess is fully torn down after disconnect.
    assert client._process is None
    assert client._connected is False


@pytest.mark.asyncio
async def test_unknown_tool_returns_is_error_without_crashing():
    client = McpClient(_stdio_config())
    try:
        await client.connect()
        result = await client.call_tool("nope", {})
        assert result["is_error"] is True
    finally:
        await client.disconnect()


@pytest.mark.asyncio
async def test_call_before_connect_fails_closed():
    client = McpClient(_stdio_config())
    with pytest.raises(RuntimeError):
        await client.call_tool("echo", {"text": "x"})
    await client.disconnect()


@pytest.mark.asyncio
async def test_subprocess_environment_does_not_leak_desktop_secrets():
    from modus.mcp_client import mcp_subprocess_environment

    env = mcp_subprocess_environment(_stdio_config(), environ={"MODUS_API_KEY": "secret"})
    assert "MODUS_API_KEY" not in env
    # A non-allowlisted secret never reaches the child.
    env2 = mcp_subprocess_environment(_stdio_config(), environ={"HOMEBREW_SECRET": "s3cr3t"})
    assert "HOMEBREW_SECRET" not in env2
