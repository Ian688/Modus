"""Read-only Git readiness planning for future writable Peri workers."""

from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from typing import Any


_SAFE_BRANCH = re.compile(r"[^a-z0-9-]+")


async def _git(cwd: Path, *args: str) -> tuple[str, str, int]:
    try:
        process = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5.0)
        return (
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
            int(process.returncode or 0),
        )
    except (FileNotFoundError, asyncio.TimeoutError):
        return "", "git unavailable", 127


def _dirty_manifest(raw: str, *, limit: int = 500) -> tuple[list[dict[str, str]], bool]:
    records = raw.split("\0")
    manifest: list[dict[str, str]] = []
    index = 0
    while index < len(records) and len(manifest) < limit:
        record = records[index]
        index += 1
        if not record or len(record) < 4:
            continue
        status = record[:2]
        path = record[3:]
        item = {"status": status, "path": path, "kind": "modified"}
        if status == "??":
            item["kind"] = "untracked"
        elif status[0] not in {" ", "?"} and status[1] not in {" ", "?"}:
            item["kind"] = "staged_and_unstaged"
        elif status[0] not in {" ", "?"}:
            item["kind"] = "staged"
        elif status[1] not in {" ", "?"}:
            item["kind"] = "unstaged"
        if "R" in status or "C" in status:
            if index < len(records):
                item["previous_path"] = records[index]
                index += 1
            item["kind"] = "renamed" if "R" in status else "copied"
        manifest.append(item)
    return manifest, any(records[index:])


def _worktree_summary(raw: str) -> tuple[set[str], list[str]]:
    paths: set[str] = set()
    branches: list[str] = []
    for line in raw.splitlines():
        if line.startswith("worktree "):
            paths.add(str(Path(line.removeprefix("worktree ")).resolve()))
        elif line.startswith("branch refs/heads/"):
            branches.append(line.removeprefix("branch refs/heads/"))
    return paths, branches


async def inspect_git_readiness(
    cwd: str | Path, *, worker_count: int, plan_id: str,
    data_root: str | Path,
) -> dict[str, Any]:
    """Inspect and plan only; never create refs, commits, directories or worktrees."""
    workspace = Path(cwd).expanduser().resolve()
    blockers: list[dict[str, Any]] = []
    if not 1 <= int(worker_count) <= 8:
        raise ValueError("worker_count must be between 1 and 8")

    inside, _stderr, inside_code = await _git(workspace, "rev-parse", "--is-inside-work-tree")
    if inside_code != 0 or inside.strip() != "true":
        return {
            "ready": False, "repository": {"name": workspace.name},
            "dirty_manifest": [], "dirty_manifest_truncated": False,
            "workers": [],
            "blockers": [{
                "code": "not_git_repository",
                "message": "当前工作目录不是已有 Git 工作树；Modus 不会自动 git init。",
            }],
            "approval_gates": ["create_worktrees", "merge_changes"],
            "policy": {"push": "disabled", "force_cleanup": "disabled"},
        }

    root_text, _, root_code = await _git(workspace, "rev-parse", "--show-toplevel")
    root = Path(root_text.strip()).resolve() if root_code == 0 and root_text.strip() else workspace
    head, _, head_code = await _git(root, "rev-parse", "--verify", "HEAD")
    branch, _, branch_code = await _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
    status, _, status_code = await _git(
        root, "status", "--porcelain=v1", "-z", "--untracked-files=all",
    )
    worktrees, _, worktrees_code = await _git(root, "worktree", "list", "--porcelain")
    manifest, manifest_truncated = _dirty_manifest(status) if status_code == 0 else ([], False)
    existing_paths, existing_branches = _worktree_summary(worktrees) if worktrees_code == 0 else (set(), [])

    if head_code != 0:
        blockers.append({
            "code": "missing_head",
            "message": "仓库还没有可作为 Worker 基线的 HEAD commit。",
        })
    if branch_code != 0:
        blockers.append({
            "code": "detached_head",
            "message": "当前处于 detached HEAD；需先选择明确的基线分支。",
        })
    if status_code != 0:
        blockers.append({"code": "status_failed", "message": "无法读取 Git 工作树状态。"})
    elif manifest:
        blockers.append({
            "code": "dirty_worktree",
            "message": "主工作树含未提交内容；创建可写 Worker 前必须选择保护/纳入策略。",
            "count": len(manifest), "truncated": manifest_truncated,
        })

    safe_plan = _SAFE_BRANCH.sub("-", str(plan_id).lower()).strip("-")[:32] or "preview"
    fingerprint = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:12]
    planned_root = Path(data_root).expanduser().resolve() / "worktrees" / fingerprint / safe_plan
    workers: list[dict[str, Any]] = []
    for ordinal in range(1, int(worker_count) + 1):
        branch_name = f"modus/peri/{safe_plan}/worker-{ordinal}"
        planned_path = planned_root / f"worker-{ordinal}"
        _out, _err, ref_code = await _git(
            root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}",
        )
        branch_exists = ref_code == 0
        path_exists = planned_path.exists() or str(planned_path) in existing_paths
        if branch_exists:
            blockers.append({
                "code": "branch_collision", "worker": ordinal,
                "message": f"计划分支已存在：{branch_name}",
            })
        if path_exists:
            blockers.append({
                "code": "worktree_collision", "worker": ordinal,
                "message": f"Worker {ordinal} 的私有 worktree 位置已被占用。",
            })
        workers.append({
            "ordinal": ordinal, "branch": branch_name,
            "worktree_id": f"{fingerprint}/{safe_plan}/worker-{ordinal}",
            "branch_exists": branch_exists, "path_exists": path_exists,
        })

    return {
        "ready": not blockers,
        "repository": {
            "name": root.name, "head": head.strip() if head_code == 0 else "",
            "branch": branch.strip() if branch_code == 0 else "",
            "existing_worktree_count": len(existing_paths),
            "existing_worktree_branches": existing_branches,
        },
        "dirty_manifest": manifest,
        "dirty_manifest_truncated": manifest_truncated,
        "workers": workers,
        "blockers": blockers,
        "approval_gates": ["create_worktrees", "merge_changes"],
        "policy": {
            "source": "current HEAD only", "push": "disabled",
            "force_cleanup": "disabled", "automatic_merge": "disabled",
            "worker_scope": "one private worktree and branch per worker",
        },
    }
