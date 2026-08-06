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
