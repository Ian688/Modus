from pathlib import Path

import pytest

from modus.config import ModusConfig
from modus.tools.base import ToolContext
from modus.tools.builtins import (
    edit_file, get_builtin_tools, glob_files, grep, list_dir, read_file, search_code, write_file,
)


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
async def test_read_file_refuses_multimegabyte_files(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    big = workspace / "big.txt"
    big.write_bytes(b"x" * (1_000_001))
    ctx = context(workspace)

    result = await read_file({"path": "big.txt"}, ctx)

    assert result.is_error is True
    assert ">1MB" in result.content
    assert "grep" in result.content


@pytest.mark.asyncio
async def test_read_file_bounds_line_selection_and_disclosure(guard_home):
    workspace = guard_home / "workspace"
    workspace.mkdir()
    small = workspace / "small.txt"
    small.write_text("\n".join(f"line {i}" for i in range(1, 101)), encoding="utf-8")
    ctx = context(workspace)

    result = await read_file({"path": "small.txt", "offset": 2, "limit": 3}, ctx)

    assert not result.is_error
    assert result.content.startswith("2: line 2")
    assert result.content.endswith("4: line 4")
    # Disclosure counts only the selected slice, not the whole file.
    assert result.disclosure["model_bytes_sent"] == len("line 2\nline 3\nline 4")


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


@pytest.mark.asyncio
async def test_bounded_walker_prunes_skip_dirs(tmp_path, monkeypatch):
    """The walker never descends into node_modules/.venv etc."""
    from modus.tools.builtins import _iter_bounded_files

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    (root / "a.py").mkdir(parents=True)
    (root / "a.py" / "keep.txt").write_text("keep", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "skip.txt").write_text("skip", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "skip2.txt").write_text("skip2", encoding="utf-8")

    paths = [p async for p in _iter_bounded_files(root)]

    names = [str(p) for p in paths]
    assert any("keep.txt" in name for name in names)
    assert not any("skip.txt" in name for name in names)
    assert not any("skip2.txt" in name for name in names)


@pytest.mark.asyncio
async def test_bounded_walker_stops_at_cap(tmp_path, monkeypatch):
    """The walker stops scanning after _MAX_SCAN_FILES files."""
    from modus.tools.builtins import _iter_bounded_files, _MAX_SCAN_FILES

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    for index in range(_MAX_SCAN_FILES + 500):
        (root / f"f{index}.txt").write_text("x", encoding="utf-8")

    paths = [p async for p in _iter_bounded_files(root)]

    assert len(paths) <= _MAX_SCAN_FILES


@pytest.mark.asyncio
async def test_search_code_uses_bounded_scan(tmp_path, monkeypatch):
    """search_code on a tree with skip dirs ignores them."""
    from modus.tools.builtins import search_code
    from modus.config import ModusConfig
    from modus.tools.base import ToolContext

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def find_me():\n    pass\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("find_me\n", encoding="utf-8")

    ctx = ToolContext(cwd=str(root), workspace_root=str(root), config=ModusConfig())
    result = await search_code({"query": "find_me", "path": "."}, ctx)

    assert not result.is_error
    assert "src/app.py" in result.content
    assert "node_modules" not in result.content


@pytest.mark.asyncio
async def test_capture_stream_output_caps_at_boundary():
    """_capture_stream_output kills a streaming command at the cap."""
    import asyncio
    from modus.tools.builtins import _capture_stream_output

    proc = await asyncio.create_subprocess_shell(
        "python3 -c 'import sys; sys.stdout.write(\"x\"*100000)'",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr, truncated, timed_out = await _capture_stream_output(proc, None, 30.0, cap=4096)

    assert truncated is True
    assert not timed_out
    assert len(stdout) <= 4096 + 65536  # bounded near the cap
    assert proc.returncode is not None  # process was terminated


# ── scan-cap truncation disclosure (honest truncation, no silent cap) ──


def _make_ctx_capped(root, cap=100):
    from modus.config import ModusConfig

    cfg = ModusConfig()
    cfg.tools.max_scan_files = cap
    return ToolContext(cwd=str(root), workspace_root=str(root), config=cfg)


@pytest.mark.asyncio
async def test_grep_discloses_scan_cap_truncation(tmp_path, monkeypatch):
    from modus.tools.builtins import grep

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    for index in range(150):
        (root / f"f{index}.py").write_text("needle\n" if index % 30 == 0 else "x\n",
                                            encoding="utf-8")

    ctx = _make_ctx_capped(root, cap=100)
    result = await grep({"pattern": "needle", "path": ".", "limit": 1000}, ctx)

    assert not result.is_error
    assert "needle" in result.content
    assert "扫描达上限 100" in result.content  # honest truncation disclosure


@pytest.mark.asyncio
async def test_search_code_discloses_scan_cap_truncation(tmp_path, monkeypatch):
    from modus.tools.builtins import search_code

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    for index in range(150):
        (root / f"f{index}.py").write_text("needle\n" if index % 30 == 0 else "x\n",
                                            encoding="utf-8")

    ctx = _make_ctx_capped(root, cap=100)
    result = await search_code({"query": "needle", "path": ".", "limit": 1000}, ctx)

    assert not result.is_error
    assert "needle" in result.content
    assert "扫描达上限 100" in result.content


@pytest.mark.asyncio
async def test_glob_discloses_scan_cap_truncation(tmp_path, monkeypatch):
    from modus.tools.builtins import glob_files

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    for index in range(150):
        (root / f"f{index}.py").write_text("x", encoding="utf-8")

    ctx = _make_ctx_capped(root, cap=100)
    result = await glob_files({"pattern": "**/*.py", "limit": 1000}, ctx)

    assert not result.is_error
    assert "扫描达上限 100" in result.content


@pytest.mark.asyncio
async def test_no_truncation_disclosure_when_under_cap(tmp_path, monkeypatch):
    from modus.tools.builtins import grep

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    for index in range(5):
        (root / f"f{index}.py").write_text("needle\n", encoding="utf-8")

    ctx = _make_ctx_capped(root, cap=100)
    result = await grep({"pattern": "needle", "path": ".", "limit": 1000}, ctx)

    assert not result.is_error
    assert "扫描达上限" not in result.content
    assert "needle" in result.content


@pytest.mark.asyncio
async def test_walker_on_truncate_callback_fires(tmp_path, monkeypatch):
    from modus.tools.builtins import _iter_bounded_files, _MAX_SCAN_FILES

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    for index in range(_MAX_SCAN_FILES + 500):
        (root / f"f{index}.txt").write_text("x", encoding="utf-8")

    fired = []
    paths = [p async for p in _iter_bounded_files(
        root, cap=_MAX_SCAN_FILES,
        on_truncate=lambda: fired.append(True),
    )]

    assert len(paths) <= _MAX_SCAN_FILES
    assert fired  # the cap was hit and the callback disclosed it


# ── search_code word_boundary exact-symbol mode ──


@pytest.mark.asyncio
async def test_search_code_default_is_substring(tmp_path, monkeypatch):
    from modus.tools.builtins import search_code

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("find_me()\nfind_me_again()\n", encoding="utf-8")
    ctx = ToolContext(cwd=str(root), workspace_root=str(root), config=ModusConfig())

    result = await search_code({"query": "find_me", "path": ".", "limit": 50}, ctx)
    assert "find_me_again" in result.content  # substring hits the longer name


@pytest.mark.asyncio
async def test_search_code_word_boundary_exact_symbol(tmp_path, monkeypatch):
    from modus.tools.builtins import search_code

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("find_me()\nfind_me_again()\n", encoding="utf-8")
    ctx = ToolContext(cwd=str(root), workspace_root=str(root), config=ModusConfig())

    result = await search_code(
        {"query": "find_me", "path": ".", "limit": 50, "word_boundary": True}, ctx,
    )
    assert "find_me_again" not in result.content
    assert "find_me()" in result.content


@pytest.mark.asyncio
async def test_search_code_word_boundary_case_sensitive(tmp_path, monkeypatch):
    from modus.tools.builtins import search_code

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("USER = 1\nuser = 2\n", encoding="utf-8")
    ctx = ToolContext(cwd=str(root), workspace_root=str(root), config=ModusConfig())

    upper = await search_code(
        {"query": "USER", "path": ".", "limit": 50,
         "word_boundary": True, "case_sensitive": True}, ctx,
    )
    assert "USER = 1" in upper.content
    assert "user = 2" not in upper.content


@pytest.mark.asyncio
async def test_search_code_word_boundary_regex(tmp_path, monkeypatch):
    from modus.tools.builtins import search_code

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("handler_a()\nhandler_b()\nhandler_ab()\n", encoding="utf-8")
    ctx = ToolContext(cwd=str(root), workspace_root=str(root), config=ModusConfig())

    result = await search_code(
        {"query": "handler_[ab]", "path": ".", "limit": 50,
         "regex": True, "word_boundary": True}, ctx,
    )
    assert "handler_a" in result.content
    assert "handler_b" in result.content
    assert "handler_ab" not in result.content  # underscore continues the identifier


@pytest.mark.asyncio
async def test_search_code_word_boundary_invalid_regex(tmp_path, monkeypatch):
    from modus.tools.builtins import search_code

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("x", encoding="utf-8")
    ctx = ToolContext(cwd=str(root), workspace_root=str(root), config=ModusConfig())

    result = await search_code(
        {"query": "([", "path": ".", "regex": True, "word_boundary": True}, ctx,
    )
    assert result.is_error
    assert "invalid regex" in result.content


def test_search_code_declares_word_boundary_param():
    tools = {tool.name: tool for tool in get_builtin_tools()}
    props = tools["search_code"].parameters["properties"]
    assert "word_boundary" in props
