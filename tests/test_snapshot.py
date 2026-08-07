"""Side-git snapshots: capture and restore workspace trees reversibly."""

from __future__ import annotations

from pathlib import Path

import pytest

from modus.tools.snapshot import (
    create_snapshot, list_snapshots, restore_snapshot,
    _project_key, _side_git_dir,
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / "file.txt").write_text("v1\n", encoding="utf-8")
    (repo / "sub").mkdir()
    (repo / "sub" / "keep.txt").write_text("keep\n", encoding="utf-8")
    return repo


def test_snapshot_creates_side_repo_and_commit(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr("modus.paths.data_dir", lambda env=None: tmp_path / "modus-data")

    snap = create_snapshot(str(repo), phase="pre-turn", summary="before edits")

    assert snap is not None
    assert snap.phase == "pre-turn"
    assert (tmp_path / "modus-data" / "snapshots" / _project_key(str(repo)) / ".git" / "config").exists()


def test_list_snapshots_returns_newest_first(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr("modus.paths.data_dir", lambda env=None: tmp_path / "modus-data")

    create_snapshot(str(repo), phase="pre-turn", summary="first")
    (repo / "file.txt").write_text("v2\n", encoding="utf-8")
    create_snapshot(str(repo), phase="pre-turn", summary="second")

    snaps = list_snapshots(str(repo))
    assert len(snaps) >= 2
    # Newest first: both have the same phase subject, but the ids differ and
    # the newest commit is first.
    assert snaps[0].commit_id != snaps[1].commit_id


def test_restore_rolls_back_modified_file(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setattr("modus.paths.data_dir", lambda env=None: tmp_path / "modus-data")

    create_snapshot(str(repo), phase="pre-turn", summary="baseline")
    (repo / "file.txt").write_text("MUTATED\n", encoding="utf-8")

    restored, removed = restore_snapshot(str(repo), list_snapshots(str(repo))[0].commit_id)

    assert restored >= 1
    assert (repo / "file.txt").read_text(encoding="utf-8") == "v1\n"
    assert (repo / "sub" / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_snapshot_never_touches_user_git_state(tmp_path, monkeypatch):
    """The side repo is separate: the user's own .git and index stay clean."""
    repo = _repo(tmp_path)
    monkeypatch.setattr("modus.paths.data_dir", lambda env=None: tmp_path / "modus-data")

    create_snapshot(str(repo), phase="pre-turn", summary="snap")

    # No .git directory was created inside the user's project.
    assert not (repo / ".git").exists()


@pytest.mark.asyncio
async def test_executor_snapshot_then_revert_turn_restores(tmp_path, monkeypatch):
    """A mutating run captures a snapshot; revert_turn restores the tree."""
    import asyncio

    from modus.config import ModusConfig
    from modus.tools.base import Tool, ToolContext, ToolResult, object_schema
    from modus.tools.builtins import revert_turn
    from modus.tools.executor import ToolExecutor
    from modus.tools.registry import ToolRegistry

    monkeypatch.setattr("modus.paths.data_dir", lambda env=None: tmp_path / "modus-data")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "file.txt").write_text("original\n", encoding="utf-8")

    async def write_handler(payload, _ctx):
        path = workspace / payload["path"]
        path.write_text(payload["content"], encoding="utf-8")
        return ToolResult("Wrote file")

    registry = ToolRegistry()
    registry.register(Tool(
        name="write_file", description="w", handler=write_handler,
        parameters=object_schema({"path": {}, "content": {}}, ["path", "content"]),
        required_keys=["path", "content"], is_read_only=False, danger_level="medium",
    ))
    executor = ToolExecutor(registry)
    ctx = ToolContext(
        cwd=str(workspace), workspace_root=str(workspace), config=ModusConfig(),
        run_id="run-snap-e2e",
        approval_callback=lambda _req: "approve",
    )

    call = {"id": "w1", "function": {"name": "write_file", "arguments": '{"path":"file.txt","content":"mutated\\n"}'}}
    await executor.execute_all([call], ctx)

    assert (workspace / "file.txt").read_text(encoding="utf-8") == "mutated\n"

    result = await revert_turn({"action": "list"}, ctx)
    assert not result.is_error
    assert "pre-turn" in result.content

    result = await revert_turn({"action": "restore"}, ctx)
    assert not result.is_error
    assert (workspace / "file.txt").read_text(encoding="utf-8") == "original\n"


def test_snapshot_commit_id_is_real_hash_and_restores(tmp_path, monkeypatch):
    """commit_id must be the git hash (not commit stdout), and restore works."""
    repo = _repo(tmp_path)
    monkeypatch.setattr("modus.paths.data_dir", lambda env=None: tmp_path / "modus-data")

    snap = create_snapshot(str(repo), phase="pre-turn", summary="baseline")

    # commit_id is a 40-char SHA-1 hex hash, not the commit summary line.
    assert snap is not None
    assert len(snap.commit_id) == 40
    assert all(c in "0123456789abcdef" for c in snap.commit_id)

    # Mutate, then restore using the recorded hash; the file must revert.
    (repo / "file.txt").write_text("MUTATED\n", encoding="utf-8")
    restored, removed = restore_snapshot(str(repo), snap.commit_id)
    assert (repo / "file.txt").read_text(encoding="utf-8") == "v1\n"
