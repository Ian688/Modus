import json
import stat

import pytest


def test_mcp_config_is_owner_only_atomic_and_stores_only_env_references(tmp_path, monkeypatch):
    from modus.mcp_client import McpManager, McpServerConfig

    path = tmp_path / "mcp_servers.json"
    manager = McpManager(path)
    manager.add_config(McpServerConfig(
        name="search", transport="stdio", command="search-mcp",
        env={"SEARCH_API_KEY": "env:HOST_SEARCH_KEY"},
    ))

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "env" not in manager.list_configs()[0]
    assert json.loads(path.read_text())["servers"][0]["env"] == {"SEARCH_API_KEY": "env:HOST_SEARCH_KEY"}
    monkeypatch.setenv("HOST_SEARCH_KEY", "runtime-secret")
    assert manager.get_config("search").resolved_env() == {"SEARCH_API_KEY": "runtime-secret"}
    assert list(tmp_path.glob(".mcp-servers-*.tmp")) == []


def test_mcp_config_rejects_literal_environment_secrets(tmp_path):
    import pytest

    from modus.mcp_client import McpManager, McpServerConfig

    manager = McpManager(tmp_path / "mcp_servers.json")
    with pytest.raises(ValueError, match="env:NAME"):
        manager.add_config(McpServerConfig(
            name="unsafe", transport="stdio", command="unsafe-mcp",
            env={"API_KEY": "literal-secret"},
        ))
    assert not (tmp_path / "mcp_servers.json").exists()


def test_mcp_child_receives_only_essential_and_explicit_environment():
    from modus.mcp_client import McpServerConfig, mcp_subprocess_environment

    config = McpServerConfig(
        name="search", transport="stdio", command="search-mcp",
        env={"SEARCH_KEY": "env:HOST_SEARCH_KEY"},
    )
    child = mcp_subprocess_environment(config, {
        "PATH": "/usr/bin", "HOME": "/tmp/home", "HOST_SEARCH_KEY": "allowed",
        "MODUS_API_KEY": "model-secret", "UNRELATED_SECRET": "private",
    })

    assert child == {
        "PATH": "/usr/bin", "HOME": "/tmp/home", "SEARCH_KEY": "allowed",
    }


def test_mcp_config_validates_transport_target_before_persisting(tmp_path):
    import pytest

    from modus.mcp_client import McpManager, McpServerConfig

    manager = McpManager(tmp_path / "mcp_servers.json")
    with pytest.raises(ValueError, match="requires a command"):
        manager.add_config(McpServerConfig(name="empty", transport="stdio"))
    with pytest.raises(ValueError, match=r"http\(s\)"):
        manager.add_config(McpServerConfig(
            name="remote", transport="sse", url="file:///tmp/socket",
        ))
    with pytest.raises(ValueError, match="embedded credentials"):
        manager.add_config(McpServerConfig(
            name="credential-url", transport="sse",
            url="https://user:secret@example.test/sse",
        ))
    assert manager.list_configs() == []


@pytest.mark.asyncio
async def test_sse_endpoint_handshake_is_same_origin_and_routes_messages():
    from modus.mcp_client import McpClient, McpServerConfig

    client = McpClient(McpServerConfig(
        name="remote", transport="sse", url="https://example.test/mcp/sse",
    ))
    client._sse_ready = __import__("asyncio").Event()
    await client._handle_sse_event("endpoint", "/mcp/messages?session=one")

    assert client._sse_post_url == "https://example.test/mcp/messages?session=one"
    future = __import__("asyncio").get_running_loop().create_future()
    client._pending[7] = future
    await client._handle_sse_event(
        "message", '{"jsonrpc":"2.0","id":7,"result":{"ok":true}}',
    )
    assert await future == {"ok": True}


@pytest.mark.asyncio
async def test_sse_endpoint_handshake_rejects_cross_origin_redirect():
    from modus.mcp_client import McpClient, McpServerConfig

    client = McpClient(McpServerConfig(
        name="remote", transport="sse", url="https://example.test/mcp/sse",
    ))
    with pytest.raises(ValueError, match="configured origin"):
        await client._handle_sse_event("endpoint", "https://evil.test/messages")


@pytest.mark.asyncio
async def test_initialize_notification_is_sent_before_tool_discovery(monkeypatch):
    from modus.mcp_client import McpClient, McpServerConfig

    client = McpClient(McpServerConfig(
        name="stdio", transport="stdio", command="noop",
    ))
    order = []

    async def connect_transport():
        return None

    async def request(method, params):
        order.append(method)
        if method == "initialize":
            return {"serverInfo": {"name": "fixture"}}
        if method == "tools/list":
            return {"tools": []}
        return {}

    async def notify(method, params):
        order.append(method)

    monkeypatch.setattr(client, "_connect_stdio", connect_transport)
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(client, "_notify", notify)

    await client.connect()

    assert order == ["initialize", "notifications/initialized", "tools/list"]
    assert client.connected is True


@pytest.mark.asyncio
async def test_tools_changed_notification_refreshes_without_blocking_reader(monkeypatch):
    import asyncio

    from modus.mcp_client import McpClient, McpServerConfig

    changed = asyncio.Event()
    client = McpClient(
        McpServerConfig(name="remote", transport="stdio", command="noop"),
        on_tools_changed=lambda _name: changed.set(),
    )

    async def refresh():
        client._tools = []
        return []

    monkeypatch.setattr(client, "refresh_tools", refresh)
    await client._on_message({"method": "notifications/tools/list_changed"})
    await asyncio.wait_for(changed.wait(), timeout=1)

    assert changed.is_set()
