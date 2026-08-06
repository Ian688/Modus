"""Writable Peri worker orchestration: approved worktree create → run → merge.

This wraps the two approval gates (create_worktrees, merge_changes) around the
real lifecycle from ``worktree_lifecycle``.  It is deliberately opt-in
(``FeatureConfig.writable_workers``) and leaves the read-only Peri path the
default.  Every side effect stays behind ToolExecutor approval semantics:
the Host asks, the user approves or denies, and a denial aborts the gate.
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from modus.desktop import worktree_lifecycle

logger = logging.getLogger(__name__)

ApprovalFn = Callable[[dict[str, Any]], Awaitable[str]]


def _gate(approval: ApprovalFn, name: str, description: str) -> str:
    """Ask the Host/user to approve one mutation gate; returns "allow"/"deny"."""
    return approval({
        "tool_name": name,
        "description": description,
        "input": {},
        "danger_level": "high",
        "requires_approval": True,
    })


async def prepare_worktrees(
    *,
    cwd: str,
    worker_count: int,
    plan_id: str,
    data_root: str,
    approval: ApprovalFn,
) -> dict[str, Any]:
    """Create worker worktrees behind the create_worktrees gate."""
    decision = await _gate(
        approval, "create_worktrees",
        f"为 {worker_count} 个 Peri Worker 创建私有 worktree 与分支（plan {plan_id}）",
    )
    if decision not in {"allow", "approve"}:
        return {"ok": False, "error": "create_worktrees denied"}
    result = await worktree_lifecycle.create_worker_worktrees(
        cwd, worker_count=worker_count, plan_id=plan_id, data_root=data_root,
    )
    if not result["ok"]:
        logger.warning("worktree preparation failed: %s", result.get("error"))
    return result


async def merge_worktree(
    *,
    cwd: str,
    plan_id: str,
    data_root: str,
    ordinal: int,
    approval: ApprovalFn,
) -> dict[str, Any]:
    """Merge one worker branch behind the merge_changes gate."""
    decision = await _gate(
        approval, "merge_changes",
        f"把 Worker {ordinal}（{plan_id}）的改动合并回基线分支",
    )
    if decision not in {"allow", "approve"}:
        return {"ok": False, "error": "merge_changes denied"}
    return await worktree_lifecycle.merge_worker_changes(
        cwd, plan_id=plan_id, data_root=data_root, ordinal=ordinal,
    )


async def cleanup_worktree(
    *, cwd: str, plan_id: str, data_root: str, ordinal: int,
) -> dict[str, Any]:
    """Best-effort cleanup after a merge; never silently discards dirty work."""
    return await worktree_lifecycle.remove_worker_worktree(
        cwd, plan_id=plan_id, data_root=data_root, ordinal=ordinal,
    )
