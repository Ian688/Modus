"""Runtime control plane for one user-visible Agent run.

``RunStateMachine`` guards lifecycle legality; this controller owns the
cross-cutting, per-run state that must share one source of truth: cancellation
and outstanding human approvals.  It deliberately has no WebSocket or LLM
dependency so the default Agent, Peri, and MOA use the same contract.
"""
from __future__ import annotations

import asyncio
from collections.abc import Iterable

from modus.modes import normalize_mode
from modus.runtime.state import RunState, RunStateMachine
from modus.runtime.budget import RunBudget, RunLimits


class RunController:
    """Own cancellation, approval futures, and lifecycle for one ``run_id``."""

    def __init__(self, *, run_id: str, mode: str, budget: RunBudget | None = None) -> None:
        self.run_id = run_id
        self.mode = normalize_mode(mode)
        self._machine = RunStateMachine()
        self.cancel_event = asyncio.Event()
        self._pending_approvals: dict[str, asyncio.Future[str]] = {}
        self.budget = budget or RunBudget()

    @classmethod
    def from_config(cls, *, run_id: str, mode: str, config: object) -> "RunController":
        runtime = getattr(config, "runtime", None)
        limits = RunLimits(
            max_turns=int(getattr(runtime, "max_turns", 20)),
            max_tokens=int(getattr(runtime, "max_tokens", 200_000)),
            max_wall_seconds=float(getattr(runtime, "max_wall_seconds", 600.0)),
            max_verification_attempts=int(getattr(runtime, "max_verification_attempts", 3)),
        )
        return cls(run_id=run_id, mode=mode, budget=RunBudget(limits))

    @property
    def state(self) -> RunState:
        return self._machine.state

    @property
    def is_terminal(self) -> bool:
        return self._machine.is_terminal

    @property
    def pending_approval_ids(self) -> tuple[str, ...]:
        return tuple(self._pending_approvals)

    def transition(self, target: RunState) -> RunState:
        """Apply one explicitly allowed lifecycle transition and fail closed."""
        state = self._machine.transition(target)
        if self.is_terminal:
            self._deny_all_pending()
        return state

    def _deny_all_pending(self) -> None:
        pending: Iterable[asyncio.Future[str]] = tuple(self._pending_approvals.values())
        self._pending_approvals.clear()
        for future in pending:
            if not future.done():
                future.set_result("deny")

    def register_approval(self, approval_id: str) -> asyncio.Future[str]:
        """Register a one-shot approval future, denying immediately after cancel."""
        if self.cancel_event.is_set() or self.is_terminal:
            # A detached future is sufficient: this path is already denied and
            # never needs to be awaited by the active event loop.
            future: asyncio.Future[str] = asyncio.Future()
            future.set_result("deny")
            return future
        future = asyncio.get_running_loop().create_future()
        self._pending_approvals[approval_id] = future
        return future

    def remove_approval(self, approval_id: str) -> None:
        self._pending_approvals.pop(approval_id, None)

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        """Resolve exactly one locally registered approval, once.

        The WebSocket server uses ``RunApprovalBroker`` for reconnect-safe
        routing.  This narrow helper keeps the controller's own lifecycle
        registry testable and prevents cross-run resolution by construction.
        """
        future = self._pending_approvals.get(approval_id)
        if future is None or future.done():
            return False
        future.set_result(decision)
        self.remove_approval(approval_id)
        return True

    def cancel(self) -> RunState:
        """Fail closed: signal cancellation and deny every unresolved approval."""
        self.cancel_event.set()
        pending: Iterable[asyncio.Future[str]] = tuple(self._pending_approvals.values())
        self._pending_approvals.clear()
        for future in pending:
            if not future.done():
                future.set_result("deny")

        if self.state is RunState.CREATED:
            return self.transition(RunState.CANCELLED)
        if self.state in {RunState.RUNNING, RunState.WAITING_APPROVAL}:
            return self.transition(RunState.CANCELLING)
        return self.state

    def cancel_complete(self) -> RunState:
        """Finalize a previously requested cancellation after workers are reaped."""
        if self.state is RunState.CANCELLING:
            return self.transition(RunState.CANCELLED)
        return self.state

    def pause(self) -> RunState:
        """Mark the run as parked (transport detached) without cancelling work."""
        if self.state is RunState.RUNNING:
            return self.transition(RunState.PAUSED)
        return self.state

    def resume(self) -> RunState:
        """Re-activate a parked run when its transport is rebound."""
        if self.state is RunState.PAUSED:
            return self.transition(RunState.RUNNING)
        return self.state
