"""Writable Peri worker lifecycle: private worktree creation and merge.

Readiness planning lives in ``git_readiness``.  This module executes the two
approved gates once the Host has confirmed them:

- ``create_worker_worktrees``: one private worktree + branch per worker.
- ``merge_worker_changes``: non-fast-forward merge back to the base branch.

Hard rules preserved from the readiness contract: no push, no force cleanup,
no automatic merge, and every mutating step is confined to a worktree the
Host explicitly approved.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any

from modus.desktop.git_readiness import _dirty_manifest, _git


_SAFE_BRANCH = re.compile(r"[^a-z0-9-]+")


def _branch_for(plan_id: str, ordinal: int) -> str:
    safe_plan = _SAFE_BRANCH.sub("-", str(plan_id).lower()).strip("-")[:32] or "preview"
    return f"modus/peri/{safe_plan}/worker-{ordinal}"


def _worktree_path(data_root: str | Path, root: Path, plan_id: str, ordinal: int) -> Path:
    fingerprint = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    safe_plan = _SAFE_BRANCH.sub("-", str(plan_id).lower()).strip("-")[:32] or "preview"
    return Path(data_root).expanduser().resolve() / "worktrees" / fingerprint / safe_plan / f"worker-{ordinal}"


async def create_worker_worktrees(
    cwd: str | Path,
    *,
    worker_count: int,
    plan_id: str,
    data_root: str | Path,
) -> dict[str, Any]:
    """Create one private worktree per worker from current HEAD.

    Refuses to run when the base worktree is dirty, when HEAD is missing, or
    when any planned branch/path already exists (matches readiness blockers).
    """
    workspace = Path(cwd).expanduser().resolve()
    root_text, _, root_code = await _git(workspace, "rev-parse", "--show-toplevel")
    if root_code != 0:
        return {"ok": False, "error": "not a git repository"}
    root = Path(root_text.strip()).resolve()
    head, _, head_code = await _git(root, "rev-parse", "--verify", "HEAD")
    if head_code != 0 or not head.strip():
        return {"ok": False, "error": "no HEAD baseline"}
    status, _, status_code = await _git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
    )
    if status_code == 0 and _dirty_manifest(status)[0]:
        return {"ok": False, "error": "main worktree is dirty; resolve before spawning writable workers"}

    branch_name, _, _ = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    base_branch = branch_name.strip() or "main"

    created: list[dict[str, Any]] = []
    for ordinal in range(1, int(worker_count) + 1):
        branch = _branch_for(plan_id, ordinal)
        path = _worktree_path(data_root, root, plan_id, ordinal)
        _o, _e, ref_code = await _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
        if ref_code == 0:
            return {"ok": False, "error": f"branch already exists: {branch}", "created": created}
        if path.exists():
            return {"ok": False, "error": f"worktree path exists: {path}", "created": created}
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        out, err, code = await _git(
            root, "worktree", "add", "-b", branch, str(path), base_branch,
        )
        if code != 0:
            return {"ok": False, "error": f"worktree add failed: {err[:300]}", "created": created}
        created.append({"ordinal": ordinal, "branch": branch, "path": str(path), "base": base_branch})
    return {"ok": True, "created": created, "base_branch": base_branch}


async def worktree_diff(cwd: str | Path, *, plan_id: str, data_root: str | Path, ordinal: int) -> dict[str, Any]:
    """Return the diff of one worker branch vs its base, for Host review."""
    workspace = Path(cwd).expanduser().resolve()
    root_text, _, root_code = await _git(workspace, "rev-parse", "--show-toplevel")
    if root_code != 0:
        return {"ok": False, "error": "not a git repository"}
    root = Path(root_text.strip()).resolve()
    branch = _branch_for(plan_id, ordinal)
    _o, _e, ref_code = await _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if ref_code != 0:
        return {"ok": False, "error": f"branch not found: {branch}"}
    stat, _, stat_code = await _git(root, "diff", f"{branch}^...{branch}", "--stat")
    full, _, _ = await _git(root, "diff", f"{branch}^...{branch}", "-U3")
    return {
        "ok": True, "ordinal": ordinal, "branch": branch,
        "stat": stat.strip() or "(no changes)",
        "diff": full.strip()[:6000],
    }


async def merge_worker_changes(
    cwd: str | Path,
    *,
    plan_id: str,
    data_root: str | Path,
    ordinal: int,
) -> dict[str, Any]:
    """Merge one worker branch into the base branch as a non-fast-forward commit.

    Runs inside the base worktree, so the merge is a real ref update with a
    reviewable commit.  Never pushes.  Only invoked after explicit Host approval.
    """
    workspace = Path(cwd).expanduser().resolve()
    root_text, _, root_code = await _git(workspace, "rev-parse", "--show-toplevel")
    if root_code != 0:
        return {"ok": False, "error": "not a git repository"}
    root = Path(root_text.strip()).resolve()
    branch = _branch_for(plan_id, ordinal)
    _o, _e, ref_code = await _git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    if ref_code != 0:
        return {"ok": False, "error": f"branch not found: {branch}"}
    _o, err, code = await _git(root, "merge", "--no-ff", "-m", f"modus: merge worker {ordinal} ({plan_id})", branch)
    if code != 0:
        return {"ok": False, "error": f"merge failed: {err[:400]}"}
    return {"ok": True, "branch": branch, "merged": True}


async def remove_worker_worktree(
    cwd: str | Path,
    *,
    plan_id: str,
    data_root: str | Path,
    ordinal: int,
) -> dict[str, Any]:
    """Remove a finished worker worktree and its branch (post-merge cleanup).

    Refuses to run while the worktree is dirty or the branch is unmerged, so
    cleanup never discards work silently.
    """
    workspace = Path(cwd).expanduser().resolve()
    root_text, _, root_code = await _git(workspace, "rev-parse", "--show-toplevel")
    if root_code != 0:
        return {"ok": False, "error": "not a git repository"}
    root = Path(root_text.strip()).resolve()
    branch = _branch_for(plan_id, ordinal)
    path = _worktree_path(data_root, root, plan_id, ordinal)
    status, _, status_code = await _git(path, "status", "--porcelain")
    if status_code == 0 and status.strip():
        return {"ok": False, "error": f"worktree {path} is dirty; refusing cleanup"}
    _o, _e, code = await _git(root, "worktree", "remove", str(path))
    if code != 0:
        return {"ok": False, "error": f"worktree remove failed for {path}"}
    _o, _e, _ = await _git(root, "branch", "-d", branch)
    return {"ok": True, "branch": branch}
