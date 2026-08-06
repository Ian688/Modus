"""Finite lifecycle for one user-visible Agent run.

The state machine deliberately has no FastAPI, WebSocket, or LLM dependency, so
The default Agent, MOA, and Peri enforce the same cancellation and approval
semantics.
"""
from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class InvalidRunTransition(ValueError):
    """Raised when a run attempts an illegal lifecycle transition."""

    def __init__(self, source: RunState, target: RunState) -> None:
        super().__init__(f"illegal run transition: {source.value} -> {target.value}")
        self.source = source
        self.target = target


class RunStateMachine:
    """Minimal, explicit lifecycle guard for a single run id."""

    _ALLOWED: dict[RunState, frozenset[RunState]] = {
        RunState.CREATED: frozenset({RunState.RUNNING, RunState.CANCELLED, RunState.FAILED}),
        RunState.RUNNING: frozenset({
            RunState.PAUSED,
            RunState.WAITING_APPROVAL,
            RunState.CANCELLING,
            RunState.COMPLETED,
            RunState.FAILED,
        }),
        RunState.PAUSED: frozenset({
            RunState.RUNNING,
            RunState.CANCELLING,
            RunState.COMPLETED,
            RunState.FAILED,
        }),
        RunState.WAITING_APPROVAL: frozenset({RunState.RUNNING, RunState.CANCELLING, RunState.FAILED}),
        RunState.CANCELLING: frozenset({RunState.CANCELLED, RunState.FAILED}),
        RunState.CANCELLED: frozenset(),
        RunState.COMPLETED: frozenset(),
        RunState.FAILED: frozenset(),
    }

    def __init__(self, state: RunState = RunState.CREATED) -> None:
        self.state = RunState(state)

    @property
    def is_terminal(self) -> bool:
        return self.state in {RunState.CANCELLED, RunState.COMPLETED, RunState.FAILED}

    def transition(self, target: RunState) -> RunState:
        target = RunState(target)
        if target not in self._ALLOWED[self.state]:
            raise InvalidRunTransition(self.state, target)
        self.state = target
        return self.state
