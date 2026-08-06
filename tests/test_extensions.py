import pytest

from modus.extensions import ExtensionRegistry


def test_builtin_extensions_are_public_and_truthful():
    items = ExtensionRegistry().list_public()

    assert {item["id"] for item in items} == {"builtin.tools", "builtin.skills"}
    assert all(item["enabled"] is True for item in items)
    assert all(item["status"] == "active" for item in items)


def test_unwired_extension_stays_disabled_in_public_inventory():
    registry = ExtensionRegistry()
    pending = registry.add_placeholder(kind="mcp", name="GitHub", summary="Repository access")

    item = next(item for item in registry.list_public() if item["id"] == pending.id)
    assert item["kind"] == "mcp"
    assert item["enabled"] is False
    assert item["status"] == "not_connected"


@pytest.mark.asyncio
async def test_connected_mcp_tools_are_namespaced_routed_and_require_approval(tmp_path):
    from modus.config import load_config
    from modus.extensions import ExtensionRegistry
    from modus.mcp_client import McpManager, McpServerConfig, McpTool
    from modus.tools.base import ToolContext

    class Client:
        connected = True

        def __init__(self, server_name):
            self.server_name = server_name
            self.tools = [McpTool(
                name="search", description="Search", input_schema={"type": "object"},
                server_name=server_name,
            )]

        async def call_tool(self, name, arguments):
            return {"content": f"{self.server_name}:{name}:{arguments['q']}", "is_error": False}

    manager = McpManager(tmp_path / "mcp.json")
    for name in ("alpha", "beta"):
        manager._configs[name] = McpServerConfig(name=name, transport="stdio", command="noop")
        manager._servers[name] = Client(name)
    registry = ExtensionRegistry(manager)
    tools = await registry.contribute_tools(load_config(), ".")

    assert len(tools) == 2
    assert len({tool.name for tool in tools}) == 2
    assert all(tool.name.startswith("mcp__") for tool in tools)
    assert all(tool.requires_approval for tool in tools)
    context = ToolContext(cwd=".", config=load_config())
    results = [await tool.execute({"q": "smoke"}, context) for tool in tools]
    assert {result.content.split(":", 1)[0] for result in results} == {"alpha", "beta"}


@pytest.mark.asyncio
async def test_desktop_engine_build_uses_the_process_owned_extension_registry(monkeypatch, tmp_path):
    from modus.desktop import server

    observed = {}

    async def fake_registry(**kwargs):
        observed.update(kwargs)
        return object()

    class Engine:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(server, "build_tool_registry", fake_registry)
    monkeypatch.setattr(server, "create_llm_client", lambda _config: object())
    monkeypatch.setattr(server, "QueryEngine", Engine)

    await server._build_session_engine(workspace_root=tmp_path)

    assert observed["extension_registry"] is server.extension_registry
