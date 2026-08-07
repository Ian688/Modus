"""MCP server (Phase A4): Modus exposes built-in tools to other agents.

Default is a read-only lens (safe + read-only tools only).  --allow-dangerous
registers write/exec tools but a headless call to one is still DENIED (no human
to approve an ASK).  --capabilities narrows by capability class via the
deny-first gate.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from modus.mcp_server import McpServer, _build_registry, _is_read_only_lens, _tool_schema
from modus.tools.builtins import get_builtin_tools


def _server(**kw):
    return McpServer(cwd=".", **kw)


# ── read-only lens filter ──


def test_read_only_lens_classification():
    tools = {t.name: t for t in get_builtin_tools()}
    assert _is_read_only_lens(tools["read_file"]) is True
    assert _is_read_only_lens(tools["system_probe"]) is True
    assert _is_read_only_lens(tools["bash"]) is False
    assert _is_read_only_lens(tools["write_file"]) is False


def test_default_registry_is_read_only_only():
    server = _server()
    names = server.registry.list_names()
    assert "read_file" in names
    assert "system_probe" in names
    assert "bash" not in names
    assert "write_file" not in names
    assert "spawn_process" not in names
    # Every exposed tool is auto-ALLOW under the policy.
    from modus.policy.approval import ApprovalDecision, ApprovalPolicy

    policy = ApprovalPolicy(server.config.policy)
    for name in names:
        tool = server.registry.get(name)
        assert policy.evaluate(tool) is ApprovalDecision.ALLOW, name


def test_allow_dangerous_registers_write_tools():
    server = _server(allow_dangerous=True)
    assert "write_file" in server.registry.list_names()
    assert "bash" in server.registry.list_names()


def test_capabilities_filter_narrows_tools():
    server = _server(capabilities=["filesystem"])
    names = server.registry.list_names()
    assert "read_file" in names
    assert "web_search" not in names  # network capability not granted
    assert "bash" not in names


def test_tool_schema_is_mcp_shape():
    tool = {t.name: t for t in get_builtin_tools()}["read_file"]
    schema = _tool_schema(tool)
    assert schema["name"] == "read_file"
    assert "description" in schema
    assert "inputSchema" in schema
    assert "type" in schema["inputSchema"]


# ── JSON-RPC handling ──


@pytest.mark.asyncio
async def test_initialize_returns_server_info():
    server = _server()
    resp = await server._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                 "params": {"protocolVersion": "2024-11-05"}})
    assert resp["result"]["serverInfo"]["name"] == "modus"
    assert resp["result"]["protocolVersion"] == "2024-11-05"


@pytest.mark.asyncio
async def test_tools_list_returns_exposed_tools():
    server = _server()
    resp = await server._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in resp["result"]["tools"]]
    assert "read_file" in names
    assert "bash" not in names


@pytest.mark.asyncio
async def test_call_read_tool_executes(tmp_path, monkeypatch):
    from pathlib import Path

    # PathGuard is home-anchored; point home at tmp so the workspace is inside.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "f.txt").write_text("hello", encoding="utf-8")
    server = McpServer(cwd=str(tmp_path))
    resp = await server._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                 "params": {"name": "read_file", "arguments": {"path": "f.txt"}}})
    assert resp["result"]["isError"] is False
    assert "hello" in resp["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_call_write_tool_denied_headless():
    server = _server(allow_dangerous=True)  # exposed but denied on call
    resp = await server._handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                 "params": {"name": "write_file",
                                            "arguments": {"path": "x.txt", "content": "y"}}})
    assert resp["result"]["isError"] is True
    assert "approval" in resp["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_call_not_exposed_tool():
    server = _server()
    resp = await server._handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                 "params": {"name": "bash", "arguments": {"command": "ls"}}})
    assert resp["result"]["isError"] is True
    assert "not exposed" in resp["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_unknown_method_returns_error():
    server = _server()
    resp = await server._handle({"jsonrpc": "2.0", "id": 6, "method": "nope"})
    assert "error" in resp
    assert resp["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_notification_no_response():
    server = _server()
    resp = await server._handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp is None


# ── Streamable HTTP transport (Phase A4 deep-dive) ──


def test_build_http_app_serves_mcp():
    """The HTTP app reuses the same registry + deny-first gate."""
    from fastapi.testclient import TestClient

    from modus.mcp_server import build_http_app

    app = build_http_app(cwd=".")
    client = TestClient(app)

    # GET handshake
    r = client.get("/mcp")
    assert r.status_code == 200
    assert r.json()["serverInfo"]["name"] == "modus"

    # initialize
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                  "params": {"protocolVersion": "2024-11-05"}})
    assert r.status_code == 200
    assert r.json()["result"]["protocolVersion"] == "2024-11-05"

    # tools/list — read-only lens
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "read_file" in names
    assert "bash" not in names

    # write tool call denied (headless)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "write_file",
                                             "arguments": {"path": "x", "content": "y"}}})
    assert r.json()["result"]["isError"] is True
    assert "not exposed" in r.json()["result"]["content"][0]["text"]

    # notification → 202
    r = client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202


def test_http_app_respects_capabilities_filter():
    from fastapi.testclient import TestClient

    from modus.mcp_server import build_http_app

    app = build_http_app(cwd=".", capabilities=["filesystem"])
    client = TestClient(app)
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in r.json()["result"]["tools"]]
    assert "read_file" in names
    assert "web_search" not in names
