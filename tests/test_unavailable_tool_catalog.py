from modus.tools.builtins import get_builtin_tools


def test_builtin_tool_catalog_does_not_advertise_unimplemented_snapshot_restore():
    names = {tool.name for tool in get_builtin_tools()}

    assert "revert_turn" not in names
