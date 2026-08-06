"""Run-scoped approval broker shared across transient WebSocket sessions.

A browser reconnect creates a new ``DaoSession`` while an existing agent run can
still be waiting for human approval.  Approval state therefore belongs to the
run, not the socket/session object that happened to deliver the request.
"""
from __future__ import annotations

import asyncio


class RunApprovalBroker:
    """One-shot approval futures addressed by immutable run and approval IDs."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], asyncio.Future[str]] = {}

    def register(self, run_id: str, approval_id: str, future: asyncio.Future[str]) -> None:
        key = (run_id, approval_id)
        if not run_id or not approval_id:
            raise ValueError("run_id and approval_id are required")
        if key in self._pending:
            raise ValueError("duplicate approval key")
        self._pending[key] = future

    def resolve(self, run_id: str, approval_id: str, decision: str) -> bool:
        future = self._pending.get((run_id, approval_id))
        if future is None or future.done():
            return False
        future.set_result(decision)
        return True

    def deny_run(self, run_id: str) -> int:
        denied = 0
        for key, future in tuple(self._pending.items()):
            if key[0] == run_id and not future.done():
                future.set_result("deny")
                denied += 1
        return denied

    def remove(self, run_id: str, approval_id: str) -> None:
        self._pending.pop((run_id, approval_id), None)

    def pending_count(self, run_id: str | None = None) -> int:
        if run_id is None:
            return len(self._pending)
        return sum(key[0] == run_id for key in self._pending)


approval_broker = RunApprovalBroker()
