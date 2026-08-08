"""Run-scoped approval broker shared across transient WebSocket sessions.

A browser reconnect creates a new ``DaoSession`` while an existing agent run can
still be waiting for human approval.  Approval state therefore belongs to the
run, not the socket/session object that happened to deliver the request.

Unattended timeout (T3): ``register`` arms a ``call_later`` timer so an
unanswered approval auto-denies after ``runtime.approval_timeout_seconds``
instead of hanging the run forever.  The timeout resolves the future with
``"deny"`` — the run keeps going and the agent can reroute — and writes an
``approval_timeout`` entry to the audit log so the denial is never silent.
"""
from __future__ import annotations

import asyncio
import logging
import time

from modus.config import load_config
from modus.policy.audit_log import AuditLog

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 600.0


def _config_timeout() -> float:
    try:
        return float(load_config().runtime.approval_timeout_seconds) or _DEFAULT_TIMEOUT
    except Exception:
        return _DEFAULT_TIMEOUT


def _audit_timeout_deny(run_id: str, approval_id: str) -> None:
    """Best-effort audit entry; a failed audit must never raise here."""
    try:
        config = load_config()
        AuditLog(config.policy.audit_log_path).record(
            tool_name="approval",
            input_data={"run_id": run_id, "approval_id": approval_id},
            outcome="deny",
            approver="system",
            cwd="",
            phase="approval",
            verification={"resolution_reason": "approval_timeout"},
        )
    except Exception:
        logger.exception("approval_timeout audit write failed")


class RunApprovalBroker:
    """One-shot approval futures addressed by immutable run and approval IDs."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, str], asyncio.Future[str]] = {}
        self._timeouts: dict[tuple[str, str], asyncio.TimerHandle] = {}
        self._registered_at: dict[tuple[str, str], float] = {}

    def register(
        self,
        run_id: str,
        approval_id: str,
        future: asyncio.Future[str],
        *,
        timeout: float | None = None,
    ) -> None:
        key = (run_id, approval_id)
        if not run_id or not approval_id:
            raise ValueError("run_id and approval_id are required")
        if key in self._pending:
            raise ValueError("duplicate approval key")
        self._pending[key] = future
        self._registered_at[key] = time.monotonic()
        timeout = _config_timeout() if timeout is None else timeout
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and timeout > 0:
            handle = loop.call_later(timeout, self._timeout_deny, key)
            self._timeouts[key] = handle

    def _timeout_deny(self, key: tuple[str, str]) -> None:
        """Auto-deny a stale approval and audit the timeout."""
        run_id, approval_id = key
        future = self._pending.get(key)
        if future is None or future.done():
            self._timeouts.pop(key, None)
            return
        future.set_result("deny")
        logger.warning(
            "approval timed out after config timeout; denied run=%s id=%s",
            run_id, approval_id,
        )
        _audit_timeout_deny(run_id, approval_id)
        self._timeouts.pop(key, None)

    def resolve(self, run_id: str, approval_id: str, decision: str) -> bool:
        key = (run_id, approval_id)
        future = self._pending.get(key)
        if future is None or future.done():
            return False
        future.set_result(decision)
        handle = self._timeouts.pop(key, None)
        if handle is not None:
            handle.cancel()
        self._registered_at.pop(key, None)
        return True

    def deny_run(self, run_id: str) -> int:
        denied = 0
        for key, future in tuple(self._pending.items()):
            if key[0] == run_id and not future.done():
                future.set_result("deny")
                denied += 1
                handle = self._timeouts.pop(key, None)
                if handle is not None:
                    handle.cancel()
                self._registered_at.pop(key, None)
        return denied

    def remove(self, run_id: str, approval_id: str) -> None:
        key = (run_id, approval_id)
        self._pending.pop(key, None)
        handle = self._timeouts.pop(key, None)
        if handle is not None:
            handle.cancel()
        self._registered_at.pop(key, None)

    def pending_age_seconds(self, run_id: str, approval_id: str) -> float | None:
        registered = self._registered_at.get((run_id, approval_id))
        if registered is None:
            return None
        return time.monotonic() - registered

    def deny_stale(
        self, *, max_age_seconds: float = 600.0, now: float | None = None,
    ) -> int:
        """Deny every pending approval older than ``max_age_seconds``.

        Watchdog-facing: lets the health loop fail closed on approvals that
        somehow outlived their ``call_later`` timer (e.g. the loop stalled).
        Each denial is audited as ``approval_timeout``.  Returns the count.
        """
        current = time.monotonic() if now is None else now
        denied = 0
        for key, future in tuple(self._pending.items()):
            registered = self._registered_at.get(key)
            if registered is None or future.done():
                continue
            if current - registered >= max_age_seconds:
                future.set_result("deny")
                denied += 1
                self._timeouts.pop(key, None)
                self._registered_at.pop(key, None)
                _audit_timeout_deny(key[0], key[1])
        return denied

    def pending_count(self, run_id: str | None = None) -> int:
        if run_id is None:
            return len(self._pending)
        return sum(key[0] == run_id for key in self._pending)


approval_broker = RunApprovalBroker()
