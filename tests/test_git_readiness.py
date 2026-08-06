from __future__ import annotations

import subprocess

import pytest

from modus.desktop.git_readiness import inspect_git_readiness


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
async def test_clean_repository_produces_non_mutating_worker_plan(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    before_status = git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    before_refs = git(repo, "show-ref")
    before_worktrees = git(repo, "worktree", "list", "--porcelain")

    result = await inspect_git_readiness(
        repo, worker_count=2, plan_id="run_123", data_root=tmp_path / "private",
    )

    assert result["ready"] is True
    assert result["repository"]["branch"] == "main"
    assert result["dirty_manifest"] == []
    assert [item["branch"] for item in result["workers"]] == [
        "modus/peri/run-123/worker-1",
        "modus/peri/run-123/worker-2",
    ]
    assert result["approval_gates"] == ["create_worktrees", "merge_changes"]
    assert result["policy"]["push"] == "disabled"
    assert not (tmp_path / "private").exists()
    assert git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before_status
    assert git(repo, "show-ref") == before_refs
    assert git(repo, "worktree", "list", "--porcelain") == before_worktrees


@pytest.mark.asyncio
async def test_dirty_repository_is_blocked_with_manifest_and_left_unchanged(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    (repo / "tracked.txt").write_text("changed\n")
    (repo / "untracked.txt").write_text("user data\n")
    before = git(repo, "status", "--porcelain=v1", "--untracked-files=all")

    result = await inspect_git_readiness(
        repo, worker_count=1, plan_id="dirty", data_root=tmp_path / "private",
    )

    assert result["ready"] is False
    assert {item["code"] for item in result["blockers"]} == {"dirty_worktree"}
    assert {item["path"] for item in result["dirty_manifest"]} == {
        "tracked.txt", "untracked.txt",
    }
    assert git(repo, "status", "--porcelain=v1", "--untracked-files=all") == before
    assert not (tmp_path / "private").exists()


@pytest.mark.asyncio
async def test_non_repository_fails_closed_without_initializing(tmp_path):
    workspace = tmp_path / "plain"
    workspace.mkdir()

    result = await inspect_git_readiness(
        workspace, worker_count=1, plan_id="plain", data_root=tmp_path / "private",
    )

    assert result["ready"] is False
    assert result["blockers"][0]["code"] == "not_git_repository"
    assert not (workspace / ".git").exists()


@pytest.mark.asyncio
async def test_legacy_git_mutators_are_fail_closed(tmp_path):
    from modus.config import ModusConfig
    from modus.tools.base import ToolContext
    from modus.tools.git_tools import (
        GIT_WORKTREE_TOOLS, SUBAGENT_GIT_TOOLS, git_init_check,
        git_merge_to_main, git_worktree_remove,
    )

    workspace = tmp_path / "plain"
    workspace.mkdir()
    context = ToolContext(cwd=str(workspace), config=ModusConfig())

    check = await git_init_check({}, context)
    merge = await git_merge_to_main({"branch": "anything"}, context)
    cleanup = await git_worktree_remove({"path": str(tmp_path / "missing")}, context)

    assert check.is_error and not (workspace / ".git").exists()
    assert merge.is_error and cleanup.is_error
    assert {tool.name for tool in GIT_WORKTREE_TOOLS} == {"git_init_check", "git_branch_diff"}
    # The subagent tool *list* now carries scoped worktree git writes for
    # writable mode; the read-only subagent *registry* still filters them out
    # (covered in test_peri_subagent_tools). Fail-closed mutators remain absent.
    assert {tool.name for tool in SUBAGENT_GIT_TOOLS} == {
        "git_status", "git_diff_work", "git_add", "git_commit",
    }
