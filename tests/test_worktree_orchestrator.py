"""Worktree orchestrator: approval gates around real worktree lifecycle."""

from __future__ import annotations

import subprocess

import pytest

from modus.desktop.worktree_orchestrator import merge_worktree, prepare_worktrees


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


async def _allow(request):
    return "allow"


async def _deny(request):
    return "deny"


@pytest.mark.asyncio
async def test_prepare_worktrees_requires_approval_and_deny_aborts(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    private = tmp_path / "private"

    denied = await prepare_worktrees(
        cwd=str(repo), worker_count=2, plan_id="gated", data_root=str(private),
        approval=_deny,
    )
    assert denied["ok"] is False
    assert "denied" in denied["error"]
    refs = git(repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
    assert "modus/peri/gated/worker-1" not in refs


@pytest.mark.asyncio
async def test_prepare_then_merge_behind_approval(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    private = tmp_path / "private"

    prepared = await prepare_worktrees(
        cwd=str(repo), worker_count=1, plan_id="flow", data_root=str(private),
        approval=_allow,
    )
    assert prepared["ok"] is True

    # Worker commits into its private worktree.
    wt = tmp_path / "private" / next(p for p in private.rglob("*worker-1") if p.is_dir())
    (wt / "feature.txt").write_text("writable worker\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "feature")

    merged = await merge_worktree(
        cwd=str(repo), plan_id="flow", data_root=str(private), ordinal=1,
        approval=_allow,
    )
    assert merged["ok"] is True
    assert "feature.txt" in git(repo, "ls-tree", "-r", "--name-only", "main")


@pytest.mark.asyncio
async def test_merge_denied_leaves_main_untouched(tmp_path):
    repo = tmp_path / "repo"
    init_repository(repo)
    private = tmp_path / "private"

    await prepare_worktrees(
        cwd=str(repo), worker_count=1, plan_id="nomerge", data_root=str(private),
        approval=_allow,
    )
    wt = tmp_path / "private" / next(p for p in private.rglob("*worker-1") if p.is_dir())
    (wt / "feature.txt").write_text("writable worker\n")
    git(wt, "add", "feature.txt")
    git(wt, "commit", "-m", "feature")

    merged = await merge_worktree(
        cwd=str(repo), plan_id="nomerge", data_root=str(private), ordinal=1,
        approval=_deny,
    )
    assert merged["ok"] is False
    assert "feature.txt" not in git(repo, "ls-tree", "-r", "--name-only", "main")
