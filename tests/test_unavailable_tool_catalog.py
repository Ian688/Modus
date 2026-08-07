from modus.tools.builtins import get_builtin_tools


def test_builtin_tool_catalog_does_not_advertise_unimplemented_snapshot_restore():
    names = {tool.name for tool in get_builtin_tools()}

    # revert_turn is now a real registered tool backed by side-git snapshots.
    assert "revert_turn" in names
    tool = next(t for t in get_builtin_tools() if t.name == "revert_turn")
    assert tool.requires_approval is True
    assert tool.danger_level == "high"
