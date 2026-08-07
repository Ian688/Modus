from modus.tools.builtins import get_builtin_tools


def test_workspace_content_tools_disclose_but_read_is_free():
    tools = {tool.name: tool for tool in get_builtin_tools()}

    for name in ("read_file", "grep", "search_code"):
        assert tools[name].data_disclosure == "workspace_content"
        # Reading is a low-risk action: no approval card, disclosure kept as an
        # audit label only. Writes/execution remain HITL-gated.
        assert tools[name].requires_approval is False


def test_workspace_metadata_tools_are_distinct_from_content_disclosure():
    tools = {tool.name: tool for tool in get_builtin_tools()}

    for name in ("list_dir", "glob"):
        assert tools[name].data_disclosure == "workspace_metadata"
        assert tools[name].requires_approval is False


def test_safe_shell_env_filters_secrets():
    """bash subprocess env must not carry credential-named variables."""
    import os
    from modus.tools.builtins import _safe_shell_env

    os.environ["MODUS_API_KEY"] = "sk-secret"
    os.environ["OPENAI_API_KEY"] = "sk-openai"
    os.environ["PATH"] = "/usr/bin"

    safe = _safe_shell_env()
    assert "MODUS_API_KEY" not in safe
    assert "OPENAI_API_KEY" not in safe
    assert "PATH" in safe
    assert safe["PATH"] == "/usr/bin"


def test_scan_cap_config_override():
    """ToolsConfig.max_scan_files flows into the walker cap."""
    from modus.config import ModusConfig
    from modus.tools.base import ToolContext
    from modus.tools.builtins import _scan_cap

    cfg = ModusConfig()
    cfg.tools.max_scan_files = 12345
    ctx = ToolContext(cwd=".", config=cfg)
    assert _scan_cap(ctx) == 12345

    # Defaults clamp to the builtin default.
    default_ctx = ToolContext(cwd=".", config=ModusConfig())
    assert _scan_cap(default_ctx) == 20_000
