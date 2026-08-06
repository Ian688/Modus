from pathlib import Path

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.builtins import edit_file, glob_files, grep, list_dir, read_file, search_code, write_file


@pytest.fixture
def guard_home(tmp_path, monkeypatch):
    """Point Path.home() at a temp dir so the home-anchored guard has scope."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    return home


def context(workspace: Path) -> ToolContext:
    return ToolContext(cwd=str(workspace), workspace_root=str(workspace), config=ModusConfig())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "payload"),
    [
        (list_dir, {"path": "../.."}),
        (read_file, {"path": "../../outside-home/secret.txt"}),
        (write_file, {"path": "../../outside-home/new.txt", "content": "blocked"}),
        (edit_file, {"path": "../../outside-home/secret.txt", "old_text": "secret", "new_text": "changed"}),
        (grep, {"path": "../..", "pattern": "secret"}),
        (glob_files, {"pattern": "../.."}),
        (search_code, {"query": "secret", "path": "../.."}),
    ],
)
async def test_every_workspace_file_tool_rejects_escape_out_of_home(tmp_path, guard_home, handler, payload):
    # The home-anchored guard lets the Agent roam the whole home directory;
    # the only hard boundary is the home edge itself.  ``../..`` from a
    # workspace nested inside the fake home lands outside home and must be
    # rejected by every file tool.
    workspace = guard_home / "workspace"
    workspace.mkdir()
    outside_home = tmp_path / "outside-home"
    outside_home.mkdir()
    (outside_home / "secret.txt").write_text("secret", encoding="utf-8")

    result = await handler(payload, context(workspace))

    assert result.is_error is True
    assert "escapes home" in result.content.lower()
    assert not (outside_home / "new.txt").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "payload"),
    [
        (list_dir, {"path": "link"}),
        (read_file, {"path": "link/secret.txt"}),
        (write_file, {"path": "link/new.txt", "content": "blocked"}),
        (edit_file, {"path": "link/secret.txt", "old_text": "secret", "new_text": "changed"}),
        (grep, {"path": "link", "pattern": "secret"}),
        (glob_files, {"pattern": "link/*"}),
        (search_code, {"query": "secret", "path": "link"}),
    ],
)
async def test_every_workspace_file_tool_rejects_symlink_escape(tmp_path, guard_home, handler, payload):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    outside_home = tmp_path / "outside-home"
    outside_home.mkdir()
    (outside_home / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "link").symlink_to(outside_home, target_is_directory=True)

    result = await handler(payload, context(workspace))

    assert result.is_error is True
    assert "escapes home" in result.content.lower()
    assert not (outside_home / "new.txt").exists()


@pytest.mark.asyncio
async def test_file_tools_allow_normal_relative_workspace_paths(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    (workspace / "nested").mkdir()
    (workspace / "nested" / "source.txt").write_text("needle\n", encoding="utf-8")
    ctx = context(workspace)

    assert not (await list_dir({"path": "nested"}, ctx)).is_error
    assert "needle" in (await read_file({"path": "nested/source.txt"}, ctx)).content
    assert "needle" in (await grep({"path": "nested", "pattern": "needle"}, ctx)).content
    assert "nested/source.txt" in (await glob_files({"pattern": "nested/*.txt"}, ctx)).content
    assert not (await write_file({"path": "nested/output.txt", "content": "safe"}, ctx)).is_error
    assert (workspace / "nested" / "output.txt").read_text(encoding="utf-8") == "safe"


@pytest.mark.asyncio
async def test_write_file_is_atomic_and_preserves_existing_mode(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    target = workspace / "script.sh"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o751)

    result = await write_file({"path": "script.sh", "content": "new"}, context(workspace))
    assert result.metadata["change_type"] == "update"
    assert result.metadata["diff"].startswith("---")
    assert result.metadata["additions"] == 1

    assert result.is_error is False
    assert target.read_text(encoding="utf-8") == "new"
    assert target.stat().st_mode & 0o777 == 0o751
    assert not list(workspace.glob("*.modus-write"))


@pytest.mark.asyncio
async def test_write_file_observes_cancellation_before_start(guard_home):
    import asyncio

    workspace = guard_home / "workspace"
    workspace.mkdir()
    ctx = context(workspace)
    ctx.cancel_event = asyncio.Event()
    ctx.cancel_event.set()

    result = await write_file({"path": "new.txt", "content": "blocked"}, ctx)

    assert result.is_error is True
    assert not (workspace / "new.txt").exists()


@pytest.mark.asyncio
async def test_absolute_path_inside_home_is_allowed_but_outside_is_not(tmp_path, guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    inside = workspace / "inside.txt"
    inside.write_text("inside", encoding="utf-8")
    outside_home = tmp_path / "outside-home"
    outside_home.mkdir()
    outside_file = outside_home / "outside.txt"
    outside_file.write_text("outside", encoding="utf-8")
    ctx = context(workspace)

    inside_result = await read_file({"path": str(inside)}, ctx)
    outside_result = await read_file({"path": str(outside_file)}, ctx)

    assert inside_result.content == "1: inside"
    assert outside_result.is_error is True
    assert "escapes home" in outside_result.content.lower()


@pytest.mark.asyncio
async def test_search_code_finds_literal_matches_without_an_index(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("def Hello():\n    return 'world'\n", encoding="utf-8")
    (workspace / "README.md").write_text("Hello from docs\n", encoding="utf-8")
    (workspace / "binary.bin").write_bytes(b"\x00\xffHello")

    result = await search_code({"query": "hello", "path": "src"}, context(workspace))

    assert not result.is_error
    assert "src/app.py:1" in result.content
    assert "README.md" not in result.content
    assert "binary.bin" not in result.content


@pytest.mark.asyncio
async def test_search_code_supports_regex_context_and_limit(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    (workspace / "one.py").write_text("zero\nneedle one\ntwo\nneedle two\n", encoding="utf-8")
    (workspace / "two.py").write_text("needle three\n", encoding="utf-8")

    result = await search_code(
        {"query": r"needle \w+", "regex": True, "context_lines": 1, "limit": 2},
        context(workspace),
    )

    assert not result.is_error
    assert "one.py:2" in result.content
    assert "1: zero" in result.content
    assert "limited to 2 matches" in result.content


@pytest.mark.asyncio
async def test_search_code_rejects_empty_query(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()

    result = await search_code({"query": "   "}, context(workspace))

    assert result.is_error is True
    assert "non-empty" in result.content


@pytest.mark.asyncio
async def test_edit_file_requires_one_exact_match_and_writes_atomically(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\nneedle\nafter\n", encoding="utf-8")

    result = await edit_file(
        {"path": "app.py", "old_text": "needle", "new_text": "updated"},
        context(workspace),
    )
    assert result.metadata["operation"] == "edit"
    assert "-needle" in result.metadata["diff"]
    assert "+updated" in result.metadata["diff"]

    assert not result.is_error
    assert "replaced 1 exact match" in result.content
    assert target.read_text(encoding="utf-8") == "before\nupdated\nafter\n"
    assert not list(workspace.glob("*.modus-edit"))


@pytest.mark.asyncio
async def test_edit_file_rejects_ambiguous_match_unless_explicitly_replace_all(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("needle\nkeep\nneedle\n", encoding="utf-8")
    ctx = context(workspace)

    ambiguous = await edit_file({"path": "app.py", "old_text": "needle", "new_text": "updated"}, ctx)
    assert ambiguous.is_error is True
    assert "2 matches" in ambiguous.content
    assert target.read_text(encoding="utf-8") == "needle\nkeep\nneedle\n"

    replaced = await edit_file(
        {"path": "app.py", "old_text": "needle", "new_text": "updated", "replace_all": True, "expected_count": 2},
        ctx,
    )
    assert not replaced.is_error
    assert target.read_text(encoding="utf-8") == "updated\nkeep\nupdated\n"


@pytest.mark.asyncio
async def test_edit_file_observes_cancellation_before_commit(guard_home):
    import asyncio

    workspace = guard_home / "workspace"
    workspace.mkdir()
    target = workspace / "app.py"
    target.write_text("before\n", encoding="utf-8")
    ctx = context(workspace)
    ctx.cancel_event = asyncio.Event()
    ctx.cancel_event.set()

    result = await edit_file({"path": "app.py", "old_text": "before", "new_text": "after"}, ctx)

    assert result.is_error is True
    assert target.read_text(encoding="utf-8") == "before\n"
