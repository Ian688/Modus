"""Worktree lifecycle: real git create → write → diff → merge → cleanup."""

from __future__ import annotations

import subprocess

import pytest

from modus.desktop.worktree_lifecycle import (
    create_worker_worktrees,
    merge_worker_changes,
    remove_worker_worktree,
    worktree_diff,
)


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, check=True,
    ).stdout


def init_repository(path):
    path.mkdir()
    git(path, "init")
    git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    git(path, "config", "user.name", "Modus Test")
    git(path, "config", "user.email", "modus@example.invalid")
    (path / "tracked.txt").write_text("base\n")
    git(path, "add", "tracked.txt")
    git(path, "commit", "-m", "base")


@pytest.mark.asyncio
async def test_create_worktrees_then_merge_then_cleanup(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    private = tmp_path / "private"

    created = await create_worker_worktrees(
        repo, worker_count=2, plan_id="run_abc", data_root=private,
    )
    assert created["ok"] is True
    assert len(created["created"]) == 2
    assert created["base_branch"] == "main"

    # Each worker branch starts at the base commit.
    branches = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert "modus/peri/run-abc/worker-1" in branches
    assert "modus/peri/run-abc/worker-2" in branches

    # Worker 1 writes into its private worktree and commits.
    wt1 = tmp_path / "private" / next(p for p in private.rglob("*worker-1") if p.is_dir())
    (wt1 / "feature.txt").write_text("worker one work\n")
    git(wt1, "add", "feature.txt")
    git(wt1, "commit", "-m", "worker 1 feature")

    diff = await worktree_diff(repo, plan_id="run_abc", data_root=private, ordinal=1)
    assert diff["ok"] is True
    assert "feature.txt" in diff["stat"]

    merged = await merge_worker_changes(repo, plan_id="run_abc", data_root=private, ordinal=1)
    assert merged["ok"] is True
    main_log = git(repo, "log", "--oneline", "main", "-3")
    assert any("merge worker 1" in line or "worker 1 feature" in line for line in main_log.splitlines())
    assert "feature.txt" in git(repo, "ls-tree", "-r", "--name-only", "main")

    removed = await remove_worker_worktree(repo, plan_id="run_abc", data_root=private, ordinal=1)
    assert removed["ok"] is True
    branches_after = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert "modus/peri/run-abc/worker-1" not in branches_after
    assert not wt1.exists()


@pytest.mark.asyncio
async def test_create_worktrees_refuses_when_main_worktree_is_dirty(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    (repo / "tracked.txt").write_text("uncommitted\n")

    result = await create_worker_worktrees(
        repo, worker_count=1, plan_id="dirty", data_root=tmp_path / "private",
    )
    assert result["ok"] is False
    assert "dirty" in result["error"]
    refs = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert "modus/peri/dirty/worker-1" not in refs


@pytest.mark.asyncio
async def test_create_worktrees_refuses_collision(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    first = await create_worker_worktrees(
        repo, worker_count=1, plan_id="collide", data_root=tmp_path / "private",
    )
    assert first["ok"] is True

    second = await create_worker_worktrees(
        repo, worker_count=1, plan_id="collide", data_root=tmp_path / "private",
    )
    assert second["ok"] is False


@pytest.mark.asyncio
async def test_remove_refuses_dirty_worktree(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    created = await create_worker_worktrees(
        repo, worker_count=1, plan_id="cleanup", data_root=tmp_path / "private",
    )
    assert created["ok"] is True
    wt = tmp_path / "private" / next(p for p in (tmp_path / "private").rglob("*worker-1") if p.is_dir())
    (wt / "feature.txt").write_text("uncommitted worker work\n")

    removed = await remove_worker_worktree(
        repo, plan_id="cleanup", data_root=tmp_path / "private", ordinal=1,
    )
    assert removed["ok"] is False
    assert wt.exists()


@pytest.mark.asyncio
async def test_lifecycle_rejects_non_repository(tmp_path):
    workspace = tmp_path / "plain"
    workspace.mkdir()

    result = await create_worker_worktrees(
        workspace, worker_count=1, plan_id="x", data_root=tmp_path / "private",
    )
    assert result["ok"] is False
    assert not (workspace / ".git").exists()
